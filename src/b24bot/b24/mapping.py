"""Перевод между входом и выходом Битрикса.

Главная ловушка портала: на вход поля идут UPPER_SNAKE_CASE, на выход возвращаются
camelCase. Имена полей запроса нельзя переиспользовать для разбора ответа.
Все значения ниже сняты с живого портала, см. docs/00-portal-facts.md.
"""
from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------- статусы
# Их ПЯТЬ. Статусов «Новая» (1) и «Отклонена» (7) на портале не существует.
STATUS_TITLES: dict[int, str] = {
    2: "Ждёт выполнения",
    3: "Выполняется",
    4: "Ожидает контроля",
    5: "Завершена",
    6: "Отложена",
}
STATUS_EMOJI: dict[int, str] = {2: "⏳", 3: "▶️", 4: "👀", 5: "✅", 6: "⏸"}
STATUS_DONE = 5

# Псевдостатусы приходят в subStatus (поле фильтра STATUS), а не в status (REAL_STATUS).
SUBSTATUS_TITLES: dict[int, str] = {
    -1: "Просрочена",
    -2: "Не просмотрена",
    -3: "Почти просрочена",
}

PRIORITY_TITLES: dict[int, str] = {0: "Низкий", 1: "Средний", 2: "Высокий"}

# Поля задачи во множественном числе, а поля ФИЛЬТРА — в единственном.
# Перепутать = пустая выборка без всякой ошибки.
FILTER_FIELD_ALIASES = {"ACCOMPLICES": "ACCOMPLICE", "AUDITORS": "AUDITOR", "TAGS": "TAG"}

# UF-поля не возвращаются даже при select=["*"] — перечислять явно.
TASK_SELECT_FULL = [
    "ID", "TITLE", "DESCRIPTION", "STATUS", "REAL_STATUS", "STAGE_ID", "PRIORITY",
    "RESPONSIBLE_ID", "CREATED_BY", "CHANGED_BY", "CLOSED_BY", "GROUP_ID",
    "DEADLINE", "CREATED_DATE", "CHANGED_DATE", "CLOSED_DATE", "STATUS_CHANGED_DATE",
    "ACCOMPLICES", "AUDITORS", "TAGS", "TIME_ESTIMATE", "TIME_SPENT_IN_LOGS",
    "ALLOW_CHANGE_DEADLINE", "CHAT_ID", "PARENT_ID", "COMMENTS_COUNT",
    "UF_TASK_WEBDAV_FILES", "UF_CRM_TASK",
]
TASK_SELECT_LIST = [
    "ID", "TITLE", "STATUS", "REAL_STATUS", "STAGE_ID", "PRIORITY", "GROUP_ID",
    "RESPONSIBLE_ID", "CREATED_BY", "DEADLINE", "CREATED_DATE", "CHANGED_DATE",
    "CLOSED_DATE", "PARENT_ID",
]


def to_camel(upper_snake: str) -> str:
    """UF_TASK_WEBDAV_FILES -> ufTaskWebdavFiles, GROUP_ID -> groupId, ID -> id."""
    parts = [p for p in upper_snake.lower().split("_") if p]
    if not parts:
        return ""
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def encode_params(data: Any, prefix: str = "") -> dict[str, str]:
    """Разложить вложенную структуру в плоские ключи, как ждёт REST Битрикса.

    {"fields": {"TITLE": "x", "TAGS": ["a", "b"]}}
      -> {"fields[TITLE]": "x", "fields[TAGS][0]": "a", "fields[TAGS][1]": "b"}
    """
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            out.update(encode_params(value, f"{prefix}[{key}]" if prefix else str(key)))
    elif isinstance(data, (list, tuple)):
        for i, value in enumerate(data):
            out.update(encode_params(value, f"{prefix}[{i}]"))
    elif isinstance(data, bool):
        out[prefix] = "Y" if data else "N"
    elif data is None:
        out[prefix] = ""
    else:
        out[prefix] = str(data)
    return out


def parse_tags(raw: Any) -> list[str]:
    """TAGS возвращается ОБЪЕКТОМ, ключ — id тега, а не массивом строк.

    {"7": {"id": 7, "title": "devonbot"}} -> ["devonbot"]
    """
    if not raw:
        return []
    if isinstance(raw, dict):
        out = []
        for v in raw.values():
            if isinstance(v, dict) and v.get("title"):
                out.append(str(v["title"]))
            elif isinstance(v, str):
                out.append(v)
        return out
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return []


def as_int(value: Any) -> int | None:
    """Портал часто отдаёт числа строками: "8017", "547"."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def status_label(status: Any, deadline_passed: bool = False) -> str:
    s = as_int(status)
    if s is None:
        return "—"
    base = f"{STATUS_EMOJI.get(s, '•')} {STATUS_TITLES.get(s, f'статус {s}')}"
    return f"{base} 🔥" if deadline_passed and s != STATUS_DONE else base


def is_open(status: Any) -> bool:
    s = as_int(status)
    return s is not None and s != STATUS_DONE


def allowed_actions(task: dict[str, Any]) -> set[str]:
    """Разрешённые действия берём из блока action ответа, а не выдумываем сами.

    Проверено записью: при статусе 3 defer и renew отдают ошибку, и ровно этого
    в action нет. Блок надёжен для запретов; для разрешений — не исчерпывающий.
    """
    action = task.get("action") or {}
    return {k for k, v in action.items() if v is True} if isinstance(action, dict) else set()


def forbidden_actions(task: dict[str, Any]) -> set[str]:
    """Действия, которые портал запретил ЯВНО (`false`), а не просто не назвал.

    Отсутствие ключа и `false` — разные вещи, и разница видна только здесь.
    Блок `action` надёжен для запретов и не исчерпывающий для разрешений
    (docs/00-portal-facts.md §9.5: при статусе 2 в нём не было `start`, хотя
    вызов проходил). Поэтому дверь, у которой других входов нет, показывается
    по отсутствию запрета, а не по наличию разрешения: пропавший ключ иначе
    прячет её целиком, и человеку это выглядит как «функции нет вовсе».
    """
    action = task.get("action") or {}
    return {k for k, v in action.items() if v is False} if isinstance(action, dict) else set()
