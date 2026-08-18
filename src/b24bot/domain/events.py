"""Превращение событий Битрикса в уведомления.

Три факта с живого портала определяют всю логику (docs/00-portal-facts.md §11):

1. **Событие не содержит diff** — только ID задачи. Значит понять, ЧТО изменилось,
   можно единственным способом: дозапросить задачу и сравнить с кэшем.
2. **Системные сообщения выглядят как комментарии.** Создание задачи и смена статуса
   кладут запись в чат задачи, и на неё приходит `ONTASKCOMMENTADD`. Отличие
   единственное и надёжное: у системных `auth[user_id]` пустой.
3. **Подписка не фильтруется по группе** — приходят события всех задач портала.
   Каждое неизвестное `task_id` стоит дозапроса, поэтому чужие задачи заносятся
   в отбойники и больше не трогаются.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from b24bot.b24 import errors, mapping
from b24bot.b24.limiter import Lane
from b24bot.core.text import esc_html
from b24bot.db.pool import pool
from b24bot.domain import access
from b24bot.domain.context import issue_token

log = logging.getLogger(__name__)

NOT_OURS_TTL = timedelta(days=7)
ECHO_TTL = timedelta(seconds=60)
# Уведомление остаётся в истории чата надолго, и кнопки под ним обязаны жить
# столько же: протухший токен отвечает «диалог устарел», а человек видит обычное
# сообщение с обычными кнопками и не понимает, почему они мертвы.
NOTIFY_TOKEN_TTL = timedelta(days=30)

# «Поле не передавали» и «поле сбросили в пустое» — разные вещи: снятый срок это
# осмысленное значение None, и None как признак отсутствия здесь не годится.
_UNSET: Any = object()

DEFAULTS = {
    "task.created": False,
    "task.status_changed": True,
    "task.stage_changed": True,
    "task.comment_added": True,
    "task.responsible_changed": True,
    "task.deadline_changed": True,
    "task.completed": True,
    "task.deleted": True,
}

STATUS_VERB = {
    2: "вернул в ожидание", 3: "взял в работу", 4: "отправил на контроль",
    5: "завершил", 6: "отложил",
}

# Какие кнопки уместны под каким уведомлением. Смысл кнопки задаёт событие:
# на новую задачу отвечают «беру», на комментарий — читают обсуждение, на смену
# ответственного — передают дальше. Виды кнопок описаны в `keyboards.NOTIFY_BUTTONS`.
#
# У удалённой задачи кнопок нет вовсе: открывать, комментировать и менять уже
# нечего, а кнопка, ведущая в никуда, выглядит как поломка бота.
NOTIFY_ACTIONS: dict[str, tuple[str, ...]] = {
    "task.created": ("card", "start"),
    "task.status_changed": ("card", "stage"),
    "task.stage_changed": ("card", "stage"),
    "task.comment_added": ("discussion", "card"),
    "task.responsible_changed": ("card", "edit"),
    "task.deadline_changed": ("card", "deadline"),
    "task.completed": ("card", "renew"),
    "task.deleted": (),
}

# Полезная нагрузка кнопки: тот же формат, что у кнопок карточки и меню, плюс
# признак `notify`. По нему обработчик понимает, что нажали под уведомлением, и
# отвечает НОВЫМ сообщением, не затирая само уведомление (docs/30-bot-spec.md §7.3).
NOTIFY_PAYLOAD: dict[str, dict[str, Any]] = {
    "card": {},
    "discussion": {},
    "start": {"act": "start"},
    "renew": {"act": "renew"},
    "edit": {"act": "menu"},
    "deadline": {"act": "deadline_menu"},
    "stage": {"act": "stage_menu"},
}


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ приём
async def ingest(tenant_id: int, event: str, task_id: int | None,
                 b24_user_id: int | None, dedup_key: str) -> bool:
    """Положить событие в очередь. Возвращает False, если это дубль."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO b24_event_inbox (tenant_id, event, b24_task_id, b24_user_id, "
            "dedup_key) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (tenant_id, dedup_key) DO NOTHING RETURNING id",
            tenant_id, event, task_id, b24_user_id, dedup_key)
    return row is not None


# ------------------------------------------------------------- подавление эха
def fingerprint(field: str, new_value: Any, b24_user_id: int) -> str:
    raw = f"{field}|{new_value}|{b24_user_id}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


async def suppress_echo(tenant_id: int, task_id: int, field: str, new_value: Any,
                        b24_user_id: int) -> None:
    """Пометить своё изменение, чтобы событие о нём не вернулось в чат."""
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO b24_echo_suppress (tenant_id, b24_task_id, fingerprint, "
            "expires_at) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (tenant_id, b24_task_id, fingerprint) DO UPDATE "
            "SET expires_at = EXCLUDED.expires_at, used_at = NULL",
            tenant_id, task_id, fingerprint(field, new_value, b24_user_id),
            _now() + ECHO_TTL)


async def suppress_task_echo(tenant_id: int, task_id: int, b24_user_id: int, *,
                             status: int | None = None, stage: int | None = None,
                             responsible: int | None = None,
                             deadline: Any = _UNSET) -> None:
    """Пометить своё редактирование задачи сразу по нескольким полям.

    Нормализация значений обязана совпадать с той, по которой считает `_diff`:
    отпечаток берётся от значения, а не от его написания. Срок в `_diff` приводится
    к datetime, поэтому строку ISO из формы надо привести здесь же — иначе гашение
    молча не сработает и человек получит уведомление о собственном действии.
    """
    if status is not None:
        await suppress_echo(tenant_id, task_id, "STATUS", status, b24_user_id)
    if stage is not None:
        await suppress_echo(tenant_id, task_id, "STAGE", stage, b24_user_id)
    if responsible is not None:
        await suppress_echo(tenant_id, task_id, "RESPONSIBLE", responsible, b24_user_id)
    if deadline is not _UNSET:
        await suppress_echo(tenant_id, task_id, "DEADLINE", _dt(deadline), b24_user_id)


async def _is_echo(tenant_id: int, task_id: int, field: str, new_value: Any,
                   b24_user_id: int | None) -> bool:
    if b24_user_id is None:
        return False
    fp = fingerprint(field, new_value, b24_user_id)
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE b24_echo_suppress SET used_at = now() "
            "WHERE tenant_id = $1 AND b24_task_id = $2 AND fingerprint = $3 "
            "AND expires_at > now() AND used_at IS NULL RETURNING 1",
            tenant_id, task_id, fp)
    return row is not None


# --------------------------------------------------------------- настройки
async def is_enabled(tenant_id: int, project_id: int | None, code: str) -> bool:
    """Разрешение по цепочке: проект → теннант → системный дефолт.

    Отсутствие записи означает «наследовать выше», а не «выключено» — в mclick
    обратная трактовка стоила инцидента (инвариант И-9).
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT scope_kind, enabled FROM notification_settings "
            "WHERE tenant_id = $1 AND code = $2 "
            "AND ((scope_kind = 'project' AND scope_id = $3) OR scope_kind = 'tenant')",
            tenant_id, code, project_id or 0)
    by_scope = {r["scope_kind"]: r["enabled"] for r in rows}
    if "project" in by_scope:
        return bool(by_scope["project"])
    if "tenant" in by_scope:
        return bool(by_scope["tenant"])
    return DEFAULTS.get(code, False)


# --------------------------------------------------------------- обработка
async def process_one(row: Any) -> None:
    tenant_id, event = int(row["tenant_id"]), str(row["event"])
    task_id = row["b24_task_id"]
    actor = row["b24_user_id"]

    if task_id is None:
        await _finish(row["id"], "dropped", "событие без задачи")
        return

    # Системные записи в чате задачи приходят как комментарии. Отличаем по пустому
    # автору: иначе чат получал бы «новый комментарий» на каждое движение задачи.
    if event == "ONTASKCOMMENTADD" and actor is None:
        await _finish(row["id"], "dropped", "системное сообщение")
        return

    known = await _cached(tenant_id, int(task_id))
    if known is not None and not known["is_ours"]:
        await _finish(row["id"], "dropped", "чужая задача")
        return

    if event == "ONTASKDELETE":
        await _on_deleted(tenant_id, int(task_id), known, actor)
        await _finish(row["id"], "done")
        return

    try:
        client = await access.client_for_service(tenant_id)
        async with client:
            res = await client.call("tasks.task.get", {
                "taskId": int(task_id),
                "select": mapping.TASK_SELECT_FULL,
            }, lane=Lane.BACKGROUND)
    except errors.B24NotFound:
        await _on_deleted(tenant_id, int(task_id), known, actor)
        await _finish(row["id"], "done")
        return
    except errors.B24Error as exc:
        await _finish(row["id"], "failed", f"{exc.code}: {exc.description}"[:400])
        return

    task = res.get("task", res) if isinstance(res, dict) else {}
    group_id = mapping.as_int(task.get("groupId"))
    project = await _project_of(tenant_id, group_id)

    if project is None:
        # Отбойник: только идентификатор и флаг, без заголовка и полей.
        await _mark_not_ours(tenant_id, int(task_id), group_id)
        await _finish(row["id"], "dropped", "не наш проект")
        return

    domain = await _tenant_domain(tenant_id)
    changes = await _diff(tenant_id, int(task_id), task, known, actor, domain)
    await _upsert_cache(tenant_id, int(task_id), project["id"], group_id, task)

    portal_url = _portal_url(domain, int(task_id), task)
    for code, text in changes:
        if not await is_enabled(tenant_id, int(project["id"]), code):
            continue
        await _queue(tenant_id, int(project["id"]), int(task_id), code, text,
                     portal_url=portal_url)

    await _finish(row["id"], "done")


def _portal_url(domain: str, task_id: int, task: dict[str, Any]) -> str | None:
    """Адрес задачи на портале для кнопки-ссылки и для номера в тексте.

    Раздел в адресе — это контекст, а не проверка прав, поэтому подставляем
    ответственного: он ближе всех к тем, кто читает уведомление в чате проекта.
    """
    from b24bot.bot.views import portal_task_url

    owner = (mapping.as_int(task.get("responsibleId"))
             or mapping.as_int(task.get("createdBy")))
    return portal_task_url(domain, task_id, owner) if domain and owner else None


async def _tenant_domain(tenant_id: int) -> str:
    async with pool().acquire() as conn:
        value = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1",
                                    tenant_id)
    return str(value or "")


async def _diff(tenant_id: int, task_id: int, task: dict[str, Any], known: Any,
                actor: int | None, domain: str = "") -> list[tuple[str, str]]:
    """Что изменилось. Событие этого не сообщает — сравниваем с кэшем."""
    from b24bot.bot.views import task_ref

    who = (task.get("changedBy") and (task.get("creator") or {}).get("name")) or ""
    actor_name = esc_html(who or f"пользователь {actor}" if actor else "Битрикс24")
    title = esc_html(task.get("title") or "")
    # Номер — ссылка на задачу: из чата в неё уходят чаще, чем куда-либо ещё.
    ref = task_ref(task_id, domain=domain,
                   b24_user_id=(mapping.as_int(task.get("responsibleId"))
                                or mapping.as_int(task.get("createdBy"))))
    out: list[tuple[str, str]] = []

    # Стадия канбана — то, чем в чате меряют ход работы (docs/00-portal-facts.md §3.2),
    # и по одному статусу её не восстановить: они независимы. Поэтому она стоит
    # строкой в каждом уведомлении, кроме того, где и так названа.
    stage = mapping.as_int(task.get("stageId"))
    stage_line = "\n" + stage_note(await _stage_title(tenant_id, stage) if stage else "")

    if known is None:
        return [("task.created",
                 f"🆕 <b>{ref}</b> {title}\nСоздана задача{stage_line}")]

    status = mapping.as_int(task.get("status"))
    if (status is not None and status != known["status"]
            and not await _is_echo(tenant_id, task_id, "STATUS", status, actor)):
            verb = STATUS_VERB.get(status, "изменил статус")
            code = "task.completed" if status == mapping.STATUS_DONE \
                else "task.status_changed"
            emoji = mapping.STATUS_EMOJI.get(status, "•")
            out.append((code, f"{emoji} <b>{ref}</b> {title}\n"
                              f"{actor_name} {verb}{stage_line}"))

    if (stage not in (None, 0) and stage != known["stage_id"]
            and not await _is_echo(tenant_id, task_id, "STAGE", stage, actor)):
            stage_title = await _stage_title(tenant_id, stage)
            out.append(("task.stage_changed",
                        f"📂 <b>{ref}</b> {title}\n"
                        f"{actor_name} перенёс в «{esc_html(stage_title)}»"))

    resp = mapping.as_int(task.get("responsibleId"))
    if (resp is not None and resp != known["responsible_id"]
            and not await _is_echo(tenant_id, task_id, "RESPONSIBLE", resp, actor)):
            name = esc_html((task.get("responsible") or {}).get("name") or resp)
            out.append(("task.responsible_changed",
                        f"👤 <b>{ref}</b> {title}\n"
                        f"Ответственный: {name}{stage_line}"))

    # Сравнивать надо ОДИНАКОВЫЕ типы: из портала приходит строка ISO, в кэше лежит
    # timestamptz. Сравнение через str() не совпадало никогда, и «изменён срок»
    # улетало в чат на каждое событие.
    deadline = _dt(task.get("deadline"))
    if (deadline != known["deadline"]
            and not await _is_echo(tenant_id, task_id, "DEADLINE", deadline, actor)):
            from b24bot.bot.views import fmt_date
            when = fmt_date(deadline) if deadline else "снят"
            out.append(("task.deadline_changed",
                        f"⏰ <b>{ref}</b> {title}\n"
                        f"Срок: {esc_html(when)}{stage_line}"))
    return out


def stage_note(stage_title: str) -> str:
    """Строка о стадии для уведомления.

    Пустое название означает `STAGE_ID=0` — задача не разложена по канбану. Это
    нормальное состояние, и назвать его надо прямо: пропущенная строка читалась бы
    как «мы не знаем», а это другой смысл (`views.UNKNOWN_STAGE`).
    """
    from b24bot.bot.views import OUTSIDE_KANBAN

    return f"Стадия: {esc_html(stage_title or OUTSIDE_KANBAN)}"


async def _on_deleted(tenant_id: int, task_id: int, known: Any,
                      actor: int | None) -> None:
    """Об удалении можно сообщить только из кэша: дозапрашивать уже нечего."""
    if known is None or not known["is_ours"]:
        return
    title = esc_html(known["title"] or "")
    if await is_enabled(tenant_id, known["project_id"], "task.deleted"):
        await _queue(tenant_id, known["project_id"], task_id, "task.deleted",
                     f"🗑 <b>#{task_id}</b> {title}\nЗадача удалена")
    async with pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM task_cache WHERE tenant_id = $1 AND b24_task_id = $2",
            tenant_id, task_id)


# ------------------------------------------------------------- вспомогательное
async def _cached(tenant_id: int, task_id: int) -> Any:
    async with pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT project_id, is_ours, title, status, stage_id, responsible_id, "
            "deadline FROM task_cache WHERE tenant_id = $1 AND b24_task_id = $2",
            tenant_id, task_id)


async def _project_of(tenant_id: int, group_id: int | None) -> Any:
    if not group_id:
        return None
    async with pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT id FROM projects WHERE tenant_id = $1 AND b24_group_id = $2 "
            "AND status = 'active'", tenant_id, group_id)


async def _stage_title(tenant_id: int, stage_id: int) -> str:
    async with pool().acquire() as conn:
        value = await conn.fetchval(
            "SELECT title FROM project_stages WHERE tenant_id = $1 AND b24_stage_id = $2",
            tenant_id, stage_id)
    return str(value or f"стадия {stage_id}")


async def _mark_not_ours(tenant_id: int, task_id: int, group_id: int | None) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO task_cache (tenant_id, b24_task_id, b24_group_id, is_ours, "
            "expires_at) VALUES ($1,$2,$3,false,$4) "
            "ON CONFLICT (tenant_id, b24_task_id) DO UPDATE "
            "SET is_ours = false, expires_at = EXCLUDED.expires_at, synced_at = now()",
            tenant_id, task_id, group_id, _now() + NOT_OURS_TTL)


async def _upsert_cache(tenant_id: int, task_id: int, project_id: int,
                        group_id: int | None, task: dict[str, Any]) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_cache (tenant_id, b24_task_id, project_id, b24_group_id,
                is_ours, title, status, sub_status, stage_id, responsible_id, created_by,
                priority, deadline, created_date, changed_date, closed_date, synced_at)
            VALUES ($1,$2,$3,$4,true,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,now())
            ON CONFLICT (tenant_id, b24_task_id) DO UPDATE SET
                project_id = EXCLUDED.project_id, b24_group_id = EXCLUDED.b24_group_id,
                is_ours = true, title = EXCLUDED.title, status = EXCLUDED.status,
                sub_status = EXCLUDED.sub_status, stage_id = EXCLUDED.stage_id,
                responsible_id = EXCLUDED.responsible_id, priority = EXCLUDED.priority,
                deadline = EXCLUDED.deadline, changed_date = EXCLUDED.changed_date,
                closed_date = EXCLUDED.closed_date, synced_at = now()
            """,
            tenant_id, task_id, project_id, group_id, task.get("title"),
            mapping.as_int(task.get("status")), mapping.as_int(task.get("subStatus")),
            mapping.as_int(task.get("stageId")), mapping.as_int(task.get("responsibleId")),
            mapping.as_int(task.get("createdBy")), mapping.as_int(task.get("priority")),
            _dt(task.get("deadline")), _dt(task.get("createdDate")),
            _dt(task.get("changedDate")), _dt(task.get("closedDate")))


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def notify_markup(tenant_id: int, chat_ref: int, task_id: int, code: str,
                        portal_url: str | None) -> dict[str, Any] | None:
    """Кнопки под уведомлением. Токены свои на каждый чат: они его и авторизуют.

    Владельца у кнопки нет — нажать может любой участник чата, как у кнопок меню.
    Ни одна проверка на этом не экономится: обработчик заново сверяет привязку
    аккаунта, принадлежность задачи чату (И-3) и права в самом Битриксе.
    """
    from b24bot.bot import keyboards

    kinds = NOTIFY_ACTIONS.get(code, ())
    if not kinds:
        # Пустой набор — это решение «кнопок тут не место» (удалённая задача),
        # и ссылка на портал тогда тоже лишняя: открывать уже нечего.
        return None

    tokens: list[tuple[str, str]] = []
    for kind in kinds:
        token = await issue_token(
            "notify", tenant_id=tenant_id, chat_ref=chat_ref,
            payload={"task_id": task_id, "notify": True, **NOTIFY_PAYLOAD[kind]},
            single_use=False, ttl=NOTIFY_TOKEN_TTL)
        tokens.append((kind, token))
    return keyboards.notify_task(tokens, portal_url)


async def _queue(tenant_id: int, project_id: int, task_id: int, code: str,
                 text: str, *, portal_url: str | None = None) -> None:
    """Разложить уведомление по всем чатам, к которым привязан проект."""
    async with pool().acquire() as conn:
        targets = await conn.fetch(
            """
            SELECT b.chat_ref, t.thread_id, c.bot_ref, b.id AS binding_id
              FROM chat_bindings b
              JOIN tg_chats c ON c.id = b.chat_ref AND c.status IN ('claimed','active')
              LEFT JOIN tg_topics t ON t.id = b.topic_ref
             WHERE b.tenant_id = $1 AND b.project_id = $2 AND b.status = 'active'
            """, tenant_id, project_id)

    for t in targets:
        if t["bot_ref"] is None:
            continue
        # Токены выдаются ДО вставки: если строка не вставится из-за дедупликации,
        # неиспользованные токены просто протухнут. Обратный порядок хуже — между
        # вставкой и записью клавиатуры воркер успел бы отправить уведомление
        # без кнопок.
        markup = await notify_markup(tenant_id, int(t["chat_ref"]), task_id, code,
                                     portal_url)
        async with pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO outbox (tenant_id, bot_ref, chat_ref, thread_id, kind, "
                "text, markup, dedup_key) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
                "ON CONFLICT DO NOTHING",
                tenant_id, t["bot_ref"], t["chat_ref"], t["thread_id"], code, text,
                json.dumps(markup, ensure_ascii=False) if markup else None,
                f"{code}:{task_id}:{t['chat_ref']}:{_now():%Y%m%d%H%M}")
    log.info("уведомление %s по задаче %s поставлено в %d чат(ов)",
             code, task_id, len(targets))


async def _finish(event_id: int, state: str, error: str | None = None) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE b24_event_inbox SET state = $2, last_error = $3, "
            "attempts = attempts + 1, processed_at = now() WHERE id = $1",
            event_id, state, error)


async def take_pending(limit: int = 20) -> list[Any]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "UPDATE b24_event_inbox SET state = 'processing' WHERE id IN ("
            "  SELECT id FROM b24_event_inbox WHERE state = 'pending' "
            "  ORDER BY received_at LIMIT $1 FOR UPDATE SKIP LOCKED"
            ") RETURNING id, tenant_id, event, b24_task_id, b24_user_id", limit)
    return list(rows)
