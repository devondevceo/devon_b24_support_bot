"""Регулярная синхронизация справочников портала. Пока — стадии канбана.

Стадии проекта нам не принадлежат: колонки заводит, переименовывает и удаляет
владелец проекта в Битриксе, а событий об этом не приходит — мы подписаны только
на события задач и комментариев (`ONTASK*`). До сих пор `project_stages`
заполнялись однократно, в момент импорта проекта, и дальше расходились с порталом
молча: задачи новой колонки попадали в сводке в строку «Вне канбана», а
переименованная колонка годами показывала имя, которого в Битриксе уже нет.

Два входа, и оба нужны:

  * **плановый проход** воркера по активным проектам раз в сутки — он и держит
    справочник в тонусе;
  * **точечное обновление** из сводки, когда в задачах встретилась незнакомая
    стадия: ждать суточного прохода нельзя, человек смотрит на экран сейчас.

Фон ходит токеном установщика приложения (docs/10-architecture.md §3): стадии —
структура проекта, а не данные человека, и права здесь ничего не режут.

Отметка о синхронизации — `projects.stages_synced_at`. Колонка заведена
миграцией `0002` и до сих пор не заполнялась ни разу; новой миграции не нужно.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from b24bot.b24 import errors
from b24bot.b24.client import B24Client
from b24bot.b24.limiter import Lane
from b24bot.b24.mapping import as_int
from b24bot.b24.tokens import NeedsReauth
from b24bot.db.pool import pool, system_scope, tenant_scope
from b24bot.domain import access

log = logging.getLogger(__name__)

# Период планового обновления — сутки (docs/10-architecture.md §4, джоб sync_stages).
STAGE_TTL = timedelta(hours=24)
# Проектов за один проход. Проход идёт в том же цикле, что события и отправка
# сообщений: пачка должна кончаться быстрее, чем человек замечает задержку.
PASS_LIMIT = 5
# Портал отказал или вернул пустоту — не долбить его каждым проходом.
FAIL_COOLDOWN = timedelta(hours=1)
# Минимальный возраст справочника, при котором точечное обновление вообще имеет
# смысл. Стадия может быть незнакомой навсегда (задачу перенесли из чужого
# проекта), и без этого предела каждая сводка била бы в портал.
ON_DEMAND_TTL = timedelta(minutes=10)
# Потолок ожидания портала в живом сценарии. Уточнение справочника — не та вещь,
# ради которой человек согласен смотреть на пустой экран.
ON_DEMAND_WAIT = 8.0

# Проекты, по которым портал только что отказал. В памяти процесса намеренно:
# состояние живёт минуты, а перезапуск воркера — законный повод попробовать снова.
_failed_until: dict[tuple[int, int], datetime] = {}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Stage:
    b24_stage_id: int
    title: str
    sort: int
    system_type: str | None
    color: str | None


def parse_stages(raw: Any, b24_group_id: int) -> list[Stage]:
    """Разбор ответа `task.stages.get` (docs/00-portal-facts.md §3.2).

    Ответ приходит объектом, где ключ — id стадии; список тоже допускаем, потому
    что форма ответа портала стоила нам уже не одной ошибки.

    Стадии с чужим `ENTITY_ID` отбрасываются. Проверенный ответ содержит только
    стадии запрошенной группы (§3.2), но у этого же метода есть режим личного
    канбана, и цена ошибки — чужие колонки в сводке клиента.
    """
    items: list[Any]
    if isinstance(raw, dict):
        items = list(raw.values())
    elif isinstance(raw, list):
        items = list(raw)
    else:
        items = []

    out: list[Stage] = []
    foreign = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        stage_id = as_int(item.get("ID"))
        if not stage_id:
            continue
        entity = as_int(item.get("ENTITY_ID"))
        if entity is not None and entity != b24_group_id:
            foreign += 1
            continue
        title = str(item.get("TITLE") or "").strip() or f"Стадия {stage_id}"
        out.append(Stage(
            b24_stage_id=stage_id,
            title=title,
            sort=as_int(item.get("SORT")) or 0,
            system_type=_text(item.get("SYSTEM_TYPE")),
            color=_text(item.get("COLOR")),
        ))
    if foreign:
        log.info("группа %s: пропущено %d стадий чужой сущности", b24_group_id, foreign)
    out.sort(key=lambda s: (s.sort, s.b24_stage_id))
    return out


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def apply_stages(conn: asyncpg.Connection, tenant_id: int, project_id: int,
                       stages: list[Stage]) -> tuple[int, int]:
    """Записать стадии проекта. Портал — источник правды: чего в ответе нет, того нет.

    Удаление исчезнувших стадий делается ТОЛЬКО вместе с непустым ответом. Пустой
    ответ — это «портал промолчал», а не «колонок больше нет»: стереть по нему
    справочник значит превратить всю сводку в «Вне канбана».

    Вызывается внутри чужой транзакции (импорт проекта), поэтому соединение
    приходит аргументом, а не берётся из пула.
    """
    if not stages:
        return 0, 0

    for st in stages:
        await conn.execute(
            "INSERT INTO project_stages (tenant_id, project_id, b24_stage_id, title, "
            "sort, system_type, color) VALUES ($1,$2,$3,$4,$5,$6,$7) "
            "ON CONFLICT (tenant_id, project_id, b24_stage_id) DO UPDATE "
            "SET title = EXCLUDED.title, sort = EXCLUDED.sort, "
            "system_type = EXCLUDED.system_type, color = EXCLUDED.color, "
            "synced_at = now()",
            tenant_id, project_id, st.b24_stage_id, st.title, st.sort,
            st.system_type, st.color)

    removed = await conn.fetchval(
        "WITH gone AS ("
        "  DELETE FROM project_stages WHERE tenant_id = $1 AND project_id = $2 "
        "    AND b24_stage_id <> ALL($3::bigint[]) RETURNING 1"
        ") SELECT count(*) FROM gone",
        tenant_id, project_id, [s.b24_stage_id for s in stages])

    await conn.execute(
        "UPDATE projects SET stages_synced_at = now() WHERE tenant_id = $1 AND id = $2",
        tenant_id, project_id)
    return len(stages), int(removed or 0)


async def sync_project_stages(tenant_id: int, project_id: int, b24_group_id: int, *,
                              client: B24Client | None = None,
                              lane: Lane = Lane.BACKGROUND) -> int | None:
    """Обновить стадии одного проекта.

    Возвращает число записанных стадий; `None` — портал не ответил или ответил
    пустотой, справочник при этом не тронут.

    `client` передают там, где соединение с порталом уже открыто (импорт проекта,
    тесты). Без него берётся сервисный токен теннанта.
    """
    key = (tenant_id, project_id)
    try:
        if client is not None:
            raw = await client.call("task.stages.get", {"entityId": b24_group_id}, lane=lane)
        else:
            service = await access.client_for_service(tenant_id)
            async with service:
                raw = await service.call("task.stages.get", {"entityId": b24_group_id},
                                         lane=lane)
    except (NeedsReauth, errors.B24Error) as exc:
        log.warning("стадии проекта %s (группа %s) не обновлены: %s",
                    project_id, b24_group_id, exc)
        _failed_until[key] = _now() + FAIL_COOLDOWN
        return None

    stages = parse_stages(raw, b24_group_id)
    if not stages:
        log.warning("портал не вернул ни одной стадии для группы %s — "
                    "справочник проекта %s оставлен как есть", b24_group_id, project_id)
        _failed_until[key] = _now() + FAIL_COOLDOWN
        return None

    async with pool().acquire() as conn, conn.transaction():
        stored, removed = await apply_stages(conn, tenant_id, project_id, stages)

    _failed_until.pop(key, None)
    if removed:
        log.info("проект %s: стадий записано %d, исчезнувших удалено %d",
                 project_id, stored, removed)
    return stored


async def due_projects(limit: int) -> list[asyncpg.Record]:
    """Проекты, чей справочник стадий пора обновить. Самые заброшенные первыми."""
    with system_scope():  # обход всех теннантов (RLS)
        return await _due_projects(limit)


async def _due_projects(limit: int) -> list[asyncpg.Record]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.tenant_id, p.b24_group_id
              FROM projects p
              JOIN tenants t ON t.id = p.tenant_id AND t.status = 'active'
             WHERE p.status = 'active'
               AND (p.stages_synced_at IS NULL
                    OR p.stages_synced_at < now() - $1::interval)
             ORDER BY p.stages_synced_at ASC NULLS FIRST, p.id
             LIMIT $2
            """, STAGE_TTL, limit)
    return list(rows)


async def sync_stages_due(limit: int = PASS_LIMIT) -> int:
    """Плановый проход воркера. Возвращает число обновлённых проектов.

    Не бросает исключений вообще: цикл воркера важнее справочника стадий, а
    упавший проход обязан выглядеть как строка в логе, а не как мёртвый воркер.

    Кандидатов берём с запасом и пропускаем те, по которым портал недавно отказал:
    иначе один сломанный проект навсегда занимал бы всю пачку — он же и самый
    заброшенный, то есть всегда первый в очереди.
    """
    try:
        candidates = await due_projects(limit * 4)
    except Exception:
        log.exception("не удалось выбрать проекты для синхронизации стадий")
        return 0

    now = _now()
    attempts = 0
    updated = 0
    for row in candidates:
        if attempts >= limit:
            break
        key = (int(row["tenant_id"]), int(row["id"]))
        if _failed_until.get(key, now) > now:
            continue
        attempts += 1
        try:
            with tenant_scope(int(row["tenant_id"])):
                done = await sync_project_stages(int(row["tenant_id"]),
                                                 int(row["id"]),
                                                 int(row["b24_group_id"]))
        except Exception:
            log.exception("синхронизация стадий проекта %s упала", row["id"])
            _failed_until[key] = _now() + FAIL_COOLDOWN
            continue
        if done is not None:
            updated += 1
    if updated:
        log.info("стадии обновлены у %d проектов", updated)
    return updated


async def ensure_fresh(tenant_id: int, project_id: int, b24_group_id: int, *,
                       max_age: timedelta = ON_DEMAND_TTL) -> bool:
    """Точечное обновление: в задачах проекта встретилась незнакомая стадия.

    Возвращает True, только если справочник действительно перечитан — вызывающему
    есть смысл перечитать стадии из базы. Молчаливый False означает «слишком рано»
    или «портал недавно отказал», и это нормальный ход событий, а не ошибка.
    """
    key = (tenant_id, project_id)
    now = _now()
    if _failed_until.get(key, now) > now:
        return False

    async with pool().acquire() as conn:
        fresh = await conn.fetchval(
            "SELECT stages_synced_at > now() - $3::interval FROM projects "
            "WHERE tenant_id = $1 AND id = $2", tenant_id, project_id, max_age)
    if fresh:
        return False

    # Человек ждёт ответа прямо сейчас — фоновая полоса тут не годится, а потолок
    # ожидания обязателен: у клиента портала четыре попытки по 65 секунд, и без
    # ограничения сводка молчала бы минутами из-за необязательного уточнения.
    try:
        async with asyncio.timeout(ON_DEMAND_WAIT):
            stored = await sync_project_stages(tenant_id, project_id, b24_group_id,
                                               lane=Lane.INTERACTIVE)
    except TimeoutError:
        log.warning("стадии проекта %s не успели обновиться за %.0f с",
                    project_id, ON_DEMAND_WAIT)
        _failed_until[key] = _now() + FAIL_COOLDOWN
        return False
    return stored is not None
