"""Поля задачи Битрикса, доступные для привязки к вопросам опросника.

Список берётся с портала (`tasks.task.getFields` + `task.item.userfield.getlist`),
а не зашивается: UF-поля у каждого портала свои. Проверено на живом портале
16.08.2026, подробности — docs/00-portal-facts.md §13.

Почему список сужается allowlist-ом, а не отдаётся целиком: из 67 полей
осмысленно принять ответ человека могут единицы. `FORUM_TOPIC_ID` в выпадающем
списке настройщика — это не гибкость, а способ сломать задачу.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# Стандартные поля, куда осмысленно положить ответ на вопрос.
# DESCRIPTION намеренно отсутствует: это тело задачи, туда уходят ответы БЕЗ привязки.
STANDARD_ALLOWED = {
    "TITLE": "string",
    "PRIORITY": "enum",
    "DEADLINE": "datetime",
    "TIME_ESTIMATE": "integer",
    "TAGS": "array",
}

# UF-поля берём только тех типов, которые умеем заполнять текстом или выбором.
UF_ALLOWED_TYPES = {"string", "integer", "double", "enumeration", "date", "datetime",
                    "boolean", "url", "money"}

UF_TYPE_MAP = {
    "enumeration": "enum", "double": "integer", "url": "string",
    "money": "string", "boolean": "enum",
}

# Файлы и привязки к CRM исключены явно: у них свой сценарий (вложения) или
# свой формат идентификаторов, который человек в опроснике не наберёт.
UF_EXCLUDED = {"UF_TASK_WEBDAV_FILES", "UF_CRM_TASK", "UF_MAIL_MESSAGE"}


@dataclass
class FieldRef:
    name: str            # UPPER_SNAKE_CASE, как принимает Битрикс на вход
    title: str
    type: str            # string | enum | integer | datetime | date | array
    values: dict[str, str]   # для enum: значение -> подпись

    @property
    def is_choice_source(self) -> bool:
        """У поля есть готовый список значений — предлагаем его настройщику."""
        return bool(self.values)


async def user_fields(client: Any) -> dict[str, dict[str, Any]]:
    """UF-поля задач по имени. Только здесь видно допустимые значения списка.

    `tasks.task.getFields` для UF-поля типа `enumeration` отдаёт лишь
    `{"title": ..., "type": "enumeration"}` — без единого варианта. Варианты с их
    ID лежат в `task.item.userfield.getlist` -> `LIST`. Знать их обязательно:
    запись подписи вместо ID молча кладёт в поле `0` (docs/00-portal-facts.md §14).
    """
    try:
        rows = await client.call("task.item.userfield.getlist", {})
    except Exception as exc:
        log.warning("не удалось прочитать UF-поля задач: %s", str(exc)[:150])
        return {}
    return {str(r["FIELD_NAME"]): r for r in (rows or [])
            if isinstance(r, dict) and r.get("FIELD_NAME")}


def enum_items(meta: dict[str, Any]) -> dict[str, str]:
    """Варианты UF-списка: подпись -> ID элемента."""
    return {str(i.get("VALUE")): str(i.get("ID"))
            for i in (meta.get("LIST") or []) if isinstance(i, dict)}


async def available(client: Any) -> list[FieldRef]:
    """Поля портала, пригодные для привязки. Отсортированы по подписи."""
    raw = await client.call("tasks.task.getFields", {})
    fields = raw.get("fields", raw) if isinstance(raw, dict) else {}
    uf = await user_fields(client)

    out: list[FieldRef] = []
    for name, meta in (fields or {}).items():
        if not isinstance(meta, dict):
            continue
        title = str(meta.get("title") or name)
        values = _values_of(meta.get("values"))

        if name in STANDARD_ALLOWED:
            out.append(FieldRef(name, title, STANDARD_ALLOWED[name], values))
        elif name.startswith("UF_") and name not in UF_EXCLUDED:
            kind = str(meta.get("type") or "string")
            if kind not in UF_ALLOWED_TYPES and kind != "string":
                continue
            if kind == "enumeration" and name in uf:
                # ID элемента -> подпись: в поле уходит ID, человек видит подпись.
                values = {v: k for k, v in enum_items(uf[name]).items()}
            out.append(FieldRef(name, title, UF_TYPE_MAP.get(kind, kind), values))

    out.sort(key=lambda f: (f.name.startswith("UF_"), f.title.lower()))
    return out


# ------------------------------------------------------ создание своего поля
TRANSLIT = {
    "а": "A", "б": "B", "в": "V", "г": "G", "д": "D", "е": "E", "ё": "E",
    "ж": "ZH", "з": "Z", "и": "I", "й": "Y", "к": "K", "л": "L", "м": "M",
    "н": "N", "о": "O", "п": "P", "р": "R", "с": "S", "т": "T", "у": "U",
    "ф": "F", "х": "H", "ц": "C", "ч": "CH", "ш": "SH", "щ": "SCH", "ъ": "",
    "ы": "Y", "ь": "", "э": "E", "ю": "YU", "я": "YA",
}
NAME_PREFIX = "UF_SD_"     # SD — support desk; сразу видно, чьё это поле
NAME_MAX = 20


def field_name_for(label: str, taken: set[str]) -> str:
    """Имя поля из подписи. Битрикс принимает только `[A-Z0-9_]`, а подпись русская."""
    slug = "".join(TRANSLIT.get(ch, ch) for ch in label.lower())
    slug = re.sub(r"[^A-Za-z0-9]+", "_", slug).strip("_").upper()[:NAME_MAX] or "FIELD"
    name = f"{NAME_PREFIX}{slug}"
    if name not in taken:
        return name
    for i in range(2, 100):
        candidate = f"{name}_{i}"
        if candidate not in taken:
            return candidate
    return f"{name}_{len(taken) + 1}"


class FieldCreateError(RuntimeError):
    """Поле создать не удалось. Текст — то, что покажем настройщику."""


async def create_user_field(client: Any, label: str, *, as_list: bool,
                            options: list[dict[str, str]] | None = None
                            ) -> tuple[str, str, list[dict[str, str]]]:
    """Создать поле задачи на портале.

    Возвращает `(имя поля, наш тип, варианты с ID элементов)`.

    Поле создаётся для **всех задач портала** (`ENTITY_ID=TASKS_TASK`), а не для
    одного проекта: у задач Битрикса пользовательские поля общие. Настройщик
    обязан это видеть до нажатия — иначе он думает, что меняет один проект.
    """
    clean = label.strip()[:60]
    if not clean:
        raise FieldCreateError("Название нового поля пустое.")

    existing = await user_fields(client)
    name = field_name_for(clean, set(existing))
    kind = "enumeration" if as_list else "string"

    params: dict[str, Any] = {
        "FIELD_NAME": name,
        "USER_TYPE_ID": kind,
        "EDIT_FORM_LABEL": {"ru": clean, "en": clean},
        "LIST_COLUMN_LABEL": {"ru": clean, "en": clean},
    }
    if as_list:
        params["LIST"] = [{"VALUE": o["label"]} for o in (options or [])]

    try:
        await client.call("task.item.userfield.add", {"PARAMS": params})
    except Exception as exc:
        raise FieldCreateError(
            f"Битрикс24 отказался создавать поле: {str(exc)[:200]}") from exc

    if not as_list:
        return name, "string", []

    # Значения списка — ID элементов, а не подписи: подписью Битрикс молча пишет 0.
    fresh = await user_fields(client)
    items = enum_items(fresh.get(name, {}))
    mapped = [{"label": o["label"], "value": items.get(o["label"], o["label"])}
              for o in (options or [])]
    return name, "enum", mapped


async def sync_enum_options(client: Any, field_name: str,
                            options: list[dict[str, str]]
                            ) -> tuple[list[dict[str, str]], str]:
    """Досоздать в UF-списке недостающие варианты и вернуть их с ID элементов.

    Нужно на каждом сохранении вопроса: настройщик мог дописать вариант, которого
    в поле нет, — и ответ ушёл бы в `0` без единого сообщения об ошибке.
    """
    meta = (await user_fields(client)).get(field_name)
    if meta is None or meta.get("USER_TYPE_ID") != "enumeration":
        return options, ""

    items = enum_items(meta)
    missing = [o["label"] for o in options if o["label"] not in items]
    if missing:
        payload: list[dict[str, str]] = [{"ID": i, "VALUE": v}
                                         for v, i in items.items()]
        payload += [{"VALUE": v} for v in missing]
        try:
            await client.call("task.item.userfield.update",
                              {"ID": meta["ID"], "PARAMS": {"LIST": payload}})
            items = enum_items((await user_fields(client)).get(field_name, {}))
        except Exception as exc:
            log.warning("не удалось досоздать варианты поля %s: %s",
                        field_name, str(exc)[:150])
            return options, ("Битрикс24 не принял новые варианты списка — "
                             "проверьте поле на портале.")

    mapped = [{"label": o["label"], "value": items.get(o["label"], o["value"])}
              for o in options]
    unknown = [o["label"] for o in mapped if not str(o["value"]).isdigit()]
    if unknown:
        return mapped, ("В поле нет вариантов: " + ", ".join(unknown[:5])
                        + ". Ответы по ним не сохранятся.")
    return mapped, ""


def _values_of(raw: Any) -> dict[str, str]:
    """Список допустимых значений enum-поля.

    Формат у Битрикса не один: `PRIORITY` отдаёт словарь `{"2": "Высокий"}`,
    а `DURATION_TYPE` — плоский список `["secs", "mins", ...]`, где значение
    и подпись совпадают. На списке словарный разбор падал с
    `'list' object has no attribute 'items'` — поймано на живом портале.
    """
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return {str(v): str(v) for v in raw}
    return {}


# ------------------------------------------------------------------ значения
_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_RELATIVE = {"сегодня": 0, "завтра": 1, "послезавтра": 2}


class ConversionError(ValueError):
    """Ответ не годится для этого поля. Текст — то, что покажем человеку."""


def to_b24(field_type: str, answer: str, *, tz_offset_hours: int = 3) -> Any:
    """Ответ человека -> значение, которое примет Битрикс.

    Бросает `ConversionError`, если преобразовать нельзя. Вызывающий обязан
    поймать и положить ответ в тело задачи: молча потерять ответ нельзя,
    человек его написал.
    """
    value = answer.strip()
    if not value:
        raise ConversionError("пустой ответ")

    if field_type in ("string", "enum", "array"):
        return [value] if field_type == "array" else value

    if field_type == "integer":
        digits = re.sub(r"[^\d-]", "", value)
        if not digits or digits == "-":
            raise ConversionError(f"«{answer}» — не число")
        return int(digits)

    if field_type in ("date", "datetime"):
        return _to_datetime(value, tz_offset_hours)

    raise ConversionError(f"тип поля {field_type} не поддерживается")


def _to_datetime(value: str, tz_offset_hours: int) -> str:
    """Дата из текста. Форматы — те, которыми пишут люди, а не ISO.

    Смещение обязательно и явное: в мультитенанте у каждого портала свой часовой
    пояс, и голая дата уезжает на сутки (docs/00-portal-facts.md §3).
    """
    tz = timezone_of(tz_offset_hours)
    today = datetime.now(tz).replace(hour=18, minute=0, second=0, microsecond=0)

    low = value.lower().strip()
    if low in _RELATIVE:
        return (today + timedelta(days=_RELATIVE[low])).isoformat()

    m = re.match(r"через\s+(\d+)\s*(день|дня|дней|недел)", low)
    if m:
        days = int(m.group(1)) * (7 if m.group(2).startswith("недел") else 1)
        return (today + timedelta(days=days)).isoformat()

    m = re.match(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?"
                 r"(?:\s+(\d{1,2}):(\d{2}))?$", low)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3) or today.year)
        if year < 100:
            year += 2000
        hour = int(m.group(4)) if m.group(4) else 18
        minute = int(m.group(5)) if m.group(5) else 0
        return _build(year, month, day, hour, minute, tz, value)

    m = re.match(r"(\d{1,2})\s+([а-яё]{3})[а-яё]*(?:\s+(\d{4}))?$", low)
    if m and m.group(2) in _MONTHS:
        year = int(m.group(3) or today.year)
        return _build(year, _MONTHS[m.group(2)], int(m.group(1)), 18, 0, tz, value)

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConversionError(f"«{value}» — не похоже на дату") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.isoformat()


def _build(year: int, month: int, day: int, hour: int, minute: int,
           tz: Any, original: str) -> str:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz).isoformat()
    except ValueError as exc:
        raise ConversionError(f"«{original}» — такой даты нет") from exc


def timezone_of(offset_hours: int) -> Any:
    from datetime import timezone as _tz
    return _tz(timedelta(hours=offset_hours)) if offset_hours else UTC
