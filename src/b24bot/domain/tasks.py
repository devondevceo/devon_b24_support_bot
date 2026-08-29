"""Редактирование задачи и справочники под формы. Общий код бота и мини-аппа.

Почему отдельный модуль, а не «прямо в обработчике»: набор операций у бота и у
мини-аппа обязан совпадать (docs/50-web-and-b24-app.md), а расходятся такие вещи
ровно там, где их пишут дважды.

Правила портала, которые здесь соблюдаются (docs/00-portal-facts.md §9):

* смену ответственного делает `tasks.task.delegate`, а не сырой `update`:
  спец-методы проводят бизнес-логику и проверку прав;
* даты уходят с ЯВНЫМ offset — в мультитенанте у каждого портала свой часовой пояс;
* после записи задача перечитывается и поля сверяются: Битрикс молча игнорирует то,
  что не смог применить, и без сверки «сохранено» — это ложь в интерфейсе.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from b24bot.b24 import mapping
from b24bot.b24.client import B24Client
from b24bot.core.text import esc_bbcode
from b24bot.domain import events as b24_events

log = logging.getLogger(__name__)

TITLE_MAX = 250
DESCRIPTION_MAX = 20_000
COMMENT_MAX = 8_000

# Человеческие названия полей: попадают в отчёт «это Битрикс не принял».
FIELD_TITLES = {
    "title": "заголовок",
    "description": "описание",
    "deadline": "срок",
    "priority": "приоритет",
    "responsible_id": "ответственный",
    "stage_id": "стадия",
}
EDITABLE = frozenset(FIELD_TITLES)


class Invalid(Exception):
    """Данные формы не прошли проверку. Сообщение показывается человеку как есть."""

    def __init__(self, field: str, message: str) -> None:
        self.field, self.message = field, message
        super().__init__(f"{field}: {message}")


# ------------------------------------------------------------------- проверка
def parse_deadline(value: Any) -> str | None:
    """ISO 8601 с обязательным offset. Пустая строка означает «снять срок».

    Без offset строка трактуется порталом в часовом поясе пользователя, а у нас
    порталов много. Поэтому дата без зоны — ошибка ввода, а не повод угадывать.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise Invalid("deadline", "срок не разобрался: ожидается дата вида "
                                  "2026-08-20T18:00:00+03:00") from exc
    if parsed.tzinfo is None:
        raise Invalid("deadline", "в сроке нет часового пояса")
    if parsed.year < 2000 or parsed.year > 2100:
        raise Invalid("deadline", "срок вне разумных пределов")
    return parsed.isoformat(timespec="seconds")


def _int_field(field: str, value: Any, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise Invalid(field, "ожидается число") from exc
    if not minimum <= number <= maximum:
        raise Invalid(field, f"допустимы значения от {minimum} до {maximum}")
    return number


def validate_patch(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Нормализовать правку. В ответе только те поля, которые реально прислали.

    Отсутствие ключа и `null` — разные вещи: `deadline: ""` снимает срок,
    отсутствие ключа не трогает его вовсе (та же логика, что в И-9).
    """
    unknown = set(raw) - EDITABLE
    if unknown:
        raise Invalid(sorted(unknown)[0], "поле недоступно для редактирования")

    patch: dict[str, Any] = {}
    if "title" in raw:
        title = " ".join(str(raw["title"] or "").split())
        if not title:
            raise Invalid("title", "заголовок не может быть пустым")
        patch["title"] = title[:TITLE_MAX]
    if "description" in raw:
        # И-6 без исключений: текст человека уезжает в BBCode-поле портала.
        patch["description"] = esc_bbcode(str(raw["description"] or ""))[:DESCRIPTION_MAX]
    if "deadline" in raw:
        patch["deadline"] = parse_deadline(raw["deadline"])
    if "priority" in raw:
        patch["priority"] = _int_field("priority", raw["priority"], minimum=0, maximum=2)
    if "responsible_id" in raw:
        patch["responsible_id"] = _int_field("responsible_id", raw["responsible_id"],
                                             minimum=1, maximum=10**9)
    if "stage_id" in raw:
        patch["stage_id"] = _int_field("stage_id", raw["stage_id"], minimum=0,
                                       maximum=10**9)
    if not patch:
        raise Invalid("fields", "нечего менять")
    return patch


# ------------------------------------------------------------------ изменение
_UPDATE_FIELDS = {"title": "TITLE", "description": "DESCRIPTION",
                  "deadline": "DEADLINE", "priority": "PRIORITY",
                  "stage_id": "STAGE_ID"}


async def apply_patch(client: B24Client, tenant_id: int, task_id: int,
                      patch: Mapping[str, Any], *, actor_b24_user_id: int
                      ) -> tuple[dict[str, Any], list[str]]:
    """Применить правку. Возвращает (перечитанную задачу, список непринятых полей).

    Гашение эха ставится ДО записи: событие о нашем же изменении прилетает быстрее,
    чем мы успеваем дочитать ответ.
    """
    await b24_events.suppress_task_echo(
        tenant_id, task_id, actor_b24_user_id,
        stage=patch.get("stage_id"),
        responsible=patch.get("responsible_id"),
        **({"deadline": patch["deadline"]} if "deadline" in patch else {}))

    if "responsible_id" in patch:
        # Спец-метод, а не update: он проводит права и уведомления портала.
        await client.call("tasks.task.delegate",
                          {"taskId": task_id, "userId": patch["responsible_id"]})

    fields = {api: patch[key] for key, api in _UPDATE_FIELDS.items() if key in patch}
    if fields:
        if "DESCRIPTION" in fields:
            fields["DESCRIPTION_IN_BBCODE"] = "Y"
        await client.call("tasks.task.update", {"taskId": task_id, "fields": fields})

    fresh = await read(client, task_id)
    return fresh, _not_applied(patch, fresh)


def _not_applied(patch: Mapping[str, Any], task: Mapping[str, Any]) -> list[str]:
    """Что портал молча не принял. Проверено: неизвестные поля он игнорирует без ошибки."""
    missed: list[str] = []
    for key, wanted in patch.items():
        actual = {
            "title": task.get("title"),
            "description": task.get("description"),
            "deadline": task.get("deadline"),
            "priority": mapping.as_int(task.get("priority")),
            "responsible_id": mapping.as_int(task.get("responsibleId")),
            "stage_id": mapping.as_int(task.get("stageId")),
        }[key]
        if key == "deadline":
            same = _same_moment(wanted, actual)
        elif key in ("title", "description"):
            # Портал нормализует пробелы и переносы, поэтому сравниваем мягко.
            same = str(actual or "").strip()[:64] == str(wanted or "").strip()[:64]
        else:
            same = actual == wanted
        if not same:
            missed.append(FIELD_TITLES[key])
    return missed


def _same_moment(wanted: Any, actual: Any) -> bool:
    if not wanted:
        return not actual
    if not actual:
        return False
    try:
        return (datetime.fromisoformat(str(wanted))
                == datetime.fromisoformat(str(actual)))
    except ValueError:
        return False


async def read(client: B24Client, task_id: int) -> dict[str, Any]:
    """Задача целиком. UF-поля не приходят по `*`, поэтому список полей явный."""
    res = await client.call("tasks.task.get",
                            {"taskId": task_id, "select": mapping.TASK_SELECT_FULL})
    task = res.get("task", res) if isinstance(res, dict) else {}
    return task if isinstance(task, dict) else {}


# -------------------------------------------------------------------- сроки
def offset_of(*values: Any) -> timedelta:
    """Часовой пояс портала, вытащенный из его же дат.

    Портал отдаёт даты с собственным offset (`+03:00`), и это единственный источник
    его часового пояса, не стоящий лишнего вызова. Если дат нет — UTC, но тогда
    и предлагать «сегодня к 18:00» честнее без обещаний точности.
    """
    for value in values:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.utcoffset() or timedelta(0)
    return timedelta(0)


PRESETS = {"today": 0, "tomorrow": 1, "in3": 3, "week": 7}


def preset_deadline(kind: str, offset: timedelta, *, now: datetime | None = None,
                    hour: int = 18) -> str:
    """Быстрые сроки для кнопок бота. Пустая строка означает «снять срок»."""
    if kind == "clear":
        return ""
    if kind not in PRESETS:
        raise Invalid("deadline", "неизвестный вариант срока")
    tz = timezone(offset)
    local = (now or datetime.now(UTC)).astimezone(tz) + timedelta(days=PRESETS[kind])
    return local.replace(hour=hour, minute=0, second=0,
                         microsecond=0).isoformat(timespec="seconds")


# --------------------------------------------------------------- справочники
BATCH_USERS = 50  # столько команд принимает один batch портала


async def group_members(client: B24Client, group_id: int, *, limit: int = 100
                        ) -> list[dict[str, Any]]:
    """Участники проекта с именами. Порядок: сначала владельцы и модераторы.

    `sonet_group.user.get` отдаёт только `USER_ID` и `ROLE`, имена приходится
    добирать. Добираем одним batch-ом, а не запросом на человека: частотный лимит
    портала — 2 запроса в секунду.
    """
    raw = await client.call("sonet_group.user.get", {"ID": group_id})
    members = [m for m in (raw or []) if isinstance(m, dict)]
    ids: list[int] = []
    roles: dict[int, str] = {}
    for m in members:
        uid = mapping.as_int(m.get("USER_ID"))
        if uid is None or uid in roles:
            continue
        roles[uid] = str(m.get("ROLE") or "")
        ids.append(uid)
    ids = ids[:limit]
    if not ids:
        return []

    fetched = await client.call_many(
        [(f"u{uid}", "user.get", {"ID": uid}) for uid in ids])

    out: list[dict[str, Any]] = []
    for uid in ids:
        rows = fetched.get(f"u{uid}") or []
        row = rows[0] if isinstance(rows, list) and rows else {}
        if not isinstance(row, dict) or row.get("ACTIVE") is False:
            continue
        name = " ".join(str(row.get(k) or "") for k in ("NAME", "LAST_NAME")).strip()
        out.append({
            "id": uid,
            "name": name or f"пользователь {uid}",
            "position": str(row.get("WORK_POSITION") or ""),
            "role": roles.get(uid, ""),
        })
    out.sort(key=lambda m: (m["role"] not in ("A", "E"), m["name"].lower()))
    return out


async def user_names(client: B24Client, ids: list[int]) -> dict[int, str]:
    """Имена пользователей портала одним batch-ом.

    Отдельно от `group_members`, потому что спрашивают о разном: там — «кто в
    проекте», здесь — «как зовут вот этих». В списаниях времени встречаются и
    те, кого в проекте уже нет: человек ушёл, а его часы остались.
    """
    unique = sorted({int(i) for i in ids if i})
    if not unique:
        return {}
    fetched = await client.call_many(
        [(f"u{uid}", "user.get", {"ID": uid}) for uid in unique[:BATCH_USERS]])
    out: dict[int, str] = {}
    for uid in unique[:BATCH_USERS]:
        rows = fetched.get(f"u{uid}") or []
        row = rows[0] if isinstance(rows, list) and rows else {}
        name = ""
        if isinstance(row, dict):
            name = " ".join(str(row.get(k) or "") for k in ("NAME", "LAST_NAME")).strip()
        out[uid] = name or f"пользователь {uid}"
    return out


async def stages_of_group(client: B24Client, group_id: int) -> list[dict[str, Any]]:
    """Стадии канбана проекта. У каждого проекта они свои — общего списка нет."""
    raw = await client.call("task.stages.get", {"entityId": group_id})
    stages = list((raw or {}).values()) if isinstance(raw, dict) else []
    out: list[dict[str, Any]] = [
        {"id": mapping.as_int(s.get("ID")), "title": str(s.get("TITLE") or ""),
         "sort": mapping.as_int(s.get("SORT")) or 0}
        for s in stages if isinstance(s, dict)]
    out.sort(key=lambda s: int(s["sort"]))
    return [s for s in out if s["id"] is not None]
