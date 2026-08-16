"""Клавиатуры бота.

Три разных типа, и каждый решает свою задачу:

* **Постоянная клавиатура снизу** (`reply_markup` с `keyboard`) — только в личке с ботом.
  В группе она показалась бы всем участникам сразу и мешала бы переписке.
* **Инлайн-кнопки под сообщением** — контекстные действия: карточка задачи, выбор
  проекта, подтверждение. Живут ровно там, где нужны.
* **Закрепляемое сообщение `/help`** — инлайн-кнопки под ним работают как постоянное
  меню группы, потому что закреплённое сообщение всегда под рукой.

`callback_data` ограничен 64 байтами, поэтому туда уходит только `<ns>:<токен>`,
а полезная нагрузка лежит в `callback_tokens` (docs/30-bot-spec.md §0.2).
"""
from __future__ import annotations

from typing import Any

Button = dict[str, str]
Rows = list[list[Button]]


def inline(rows: Rows) -> dict[str, Any]:
    return {"inline_keyboard": rows}


def cb(ns: str, token: str, text: str) -> Button:
    return {"text": text, "callback_data": f"{ns}:{token}"}


def url_button(text: str, url: str) -> Button:
    return {"text": text, "url": url}


# ------------------------------------------------------- постоянная клавиатура
def persistent_private() -> dict[str, Any]:
    """Клавиатура снизу в личке с ботом. Не исчезает после нажатия."""
    return {
        "keyboard": [
            [{"text": "📊 Мои задачи"}, {"text": "🔥 Просроченные"}],
            [{"text": "🔗 Мои чаты"}, {"text": "❓ Помощь"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Напишите или выберите действие",
    }


PRIVATE_LABELS = {
    "📊 Мои задачи": "mytasks",
    "🔥 Просроченные": "overdue",
    "🔗 Мои чаты": "mychats",
    "❓ Помощь": "help",
}


# ------------------------------------------------------------ меню для группы
def help_menu(tokens: dict[str, str]) -> dict[str, Any]:
    """Кнопки под сообщением /help. Это сообщение закрепляют в чате."""
    rows: Rows = [
        [cb("m", tokens["status"], "📊 Сводка"),
         cb("m", tokens["overdue"], "🔥 Просроченные")],
        [cb("m", tokens["mine"], "👤 Мои задачи"),
         cb("m", tokens["all"], "📋 Все задачи")],
        [cb("m", tokens["new"], "➕ Создать задачу")],
    ]
    return inline(rows)


def task_list(numbers: list[tuple[str, str]], nav: list[Button] | None = None) -> dict[str, Any]:
    """Ряд кнопок-номеров под списком задач плюс навигация."""
    rows: Rows = []
    row: list[Button] = []
    for token, label in numbers:
        row.append(cb("t", token, label))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if nav:
        rows.append(nav)
    return inline(rows)


def task_card(tokens: dict[str, str], *, allowed: set[str], portal_url: str) -> dict[str, Any]:
    """Действия в карточке задачи.

    Набор строится по блоку `action` из ответа Битрикса: того, чего этому человеку
    нельзя, мы не показываем вовсе. Проверено на портале — блок надёжен для запретов.
    """
    first: list[Button] = []
    if "complete" in allowed:
        first.append(cb("a", tokens["complete"], "✅ Завершить"))
    if "start" in allowed:
        first.append(cb("a", tokens["start"], "▶️ В работу"))
    if "pause" in allowed:
        first.append(cb("a", tokens["pause"], "⏸ Пауза"))

    rows: Rows = []
    if first:
        rows.append(first)
    rows.append([cb("a", tokens["refresh"], "🔄 Обновить"),
                 url_button("🔗 Открыть в Б24", portal_url)])
    if "back" in tokens:
        rows.append([cb("m", tokens["back"], "◀️ К списку")])
    return inline(rows)


def confirm(token_yes: str, token_no: str, *, yes: str = "✅ Создать",
            no: str = "❌ Отмена") -> dict[str, Any]:
    return inline([[cb("c", token_yes, yes), cb("x", token_no, no)]])
