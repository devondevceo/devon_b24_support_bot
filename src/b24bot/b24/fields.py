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


async def available(client: Any) -> list[FieldRef]:
    """Поля портала, пригодные для привязки. Отсортированы по подписи."""
    raw = await client.call("tasks.task.getFields", {})
    fields = raw.get("fields", raw) if isinstance(raw, dict) else {}

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
            out.append(FieldRef(name, title, UF_TYPE_MAP.get(kind, kind), values))

    out.sort(key=lambda f: (f.name.startswith("UF_"), f.title.lower()))
    return out


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
