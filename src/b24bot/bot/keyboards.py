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


def web_app_button(text: str, url: str) -> dict[str, Any]:
    """Кнопка, открывающая мини-апп прямо в клиенте. ТОЛЬКО в личных чатах.

    В группе Telegram такую кнопку не принимает — там работает ссылка `t.me/…`.
    """
    return {"text": text, "web_app": {"url": url}}


# ------------------------------------------------------- постоянная клавиатура
def persistent_private(app_url: str | None = None) -> dict[str, Any]:
    """Клавиатура снизу в личке с ботом. Не исчезает после нажатия.

    Кнопка `web_app` разрешена Telegram только в личке — и именно поэтому здесь она
    несёт сам адрес мини-аппа, без короткого имени из BotFather. В группе так нельзя,
    там работает только ссылка `t.me/<бот>/<имя>?startapp=`.
    """
    app_row: list[dict[str, Any]] = (
        [{"text": "🧩 Приложение", "web_app": {"url": app_url}}] if app_url else [])
    return {
        "keyboard": [
            [{"text": "📊 Мои задачи"}, {"text": "🔥 Просроченные"}],
            [{"text": "🔗 Мои чаты"}, {"text": "❓ Помощь"}],
            [{"text": "🙋 Ожидают подтверждения"}],
            *([app_row] if app_row else []),
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Напишите или выберите действие",
    }


PRIVATE_LABELS = {
    "📊 Мои задачи": "mytasks",
    "🔥 Просроченные": "overdue",
    "🔗 Мои чаты": "mychats",
    "🙋 Ожидают подтверждения": "pending",
    "❓ Помощь": "help",
}


# ------------------------------------------------------------ меню для группы
def help_menu(tokens: dict[str, str], app_url: str | None = None) -> dict[str, Any]:
    """Кнопки под сообщением /help. Это сообщение закрепляют в чате."""
    rows: Rows = [
        [cb("m", tokens["status"], "📊 Сводка"),
         cb("m", tokens["overdue"], "🔥 Просроченные")],
        [cb("m", tokens["mine"], "👤 Мои задачи"),
         cb("m", tokens["all"], "📋 Все задачи")],
        [cb("m", tokens["new"], "➕ Создать задачу")],
    ]
    if app_url:
        rows.append([url_button("🧩 Приложение", app_url)])
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


def task_card(tokens: dict[str, str], *, allowed: set[str], portal_url: str,
              app_url: str | None = None) -> dict[str, Any]:
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

    # Стадия — отдельная кнопка карточки, а не пункт внутри «Изменить»: в чате
    # работу меряют колонками канбана, и прятать самое частое действие на второй
    # уровень меню значит делать его вдвое дороже. Право то же, что у правки, —
    # перенос идёт через `tasks.task.update`, поэтому и условие показа общее.
    second: list[Button] = [cb("a", tokens["refresh"], "🔄 Обновить")]
    if "edit" in allowed and "edit" in tokens:
        second.insert(0, cb("e", tokens["edit"], "✏️ Изменить"))
    if "edit" in allowed and "stage" in tokens:
        second.insert(0, cb("e", tokens["stage"], "📂 Стадия"))
    rows.append(second)
    links: list[Button] = [url_button("🔗 Открыть в Б24", portal_url)]
    if app_url:
        links.append(url_button("🧩 Приложение", app_url))
    rows.append(links)
    if "back" in tokens:
        rows.append([cb("m", tokens["back"], "◀️ К списку")])
    return inline(rows)


# ------------------------------------------------------- меню редактирования
def edit_menu(tokens: dict[str, str], app_url: str | None = None) -> dict[str, Any]:
    """Что можно поменять кнопками. Всё остальное — в приложении.

    Стадии здесь нет намеренно: она вынесена кнопкой на саму карточку (`task_card`).
    Одно действие живёт в одном месте — иначе меню растёт, а человек всё равно не
    знает, каким из двух путей идти.

    Свободного ввода тут нет намеренно: в группе бот не может «ждать ответа» от
    одного человека, не перехватывая чужие реплики. Произвольная дата, чек-лист и
    прочее живут в мини-аппе, где для этого есть форма.
    """
    rows: Rows = [
        [cb("e", tokens["deadline_menu"], "⏰ Срок")],
        [cb("e", tokens["assignee_menu"], "👤 Ответственный")],
        [cb("e", tokens["priority_menu"], "⚡ Приоритет")],
    ]
    if app_url:
        rows.append([url_button("🧩 Изменить в приложении", app_url)])
    rows.append([cb("e", tokens["back"], "◀️ К карточке")])
    return inline(rows)


def deadline_menu(tokens: dict[str, str], app_url: str | None = None) -> dict[str, Any]:
    rows: Rows = [
        [cb("e", tokens["today"], "Сегодня"), cb("e", tokens["tomorrow"], "Завтра")],
        [cb("e", tokens["in3"], "Через 3 дня"), cb("e", tokens["week"], "Через неделю")],
        [cb("e", tokens["clear"], "🚫 Снять срок")],
    ]
    if app_url:
        rows.append([url_button("📅 Другая дата — в приложении", app_url)])
    rows.append([cb("e", tokens["back"], "◀️ Назад")])
    return inline(rows)


def priority_menu(tokens: dict[str, str]) -> dict[str, Any]:
    return inline([
        [cb("e", tokens["p2"], "🔴 Высокий")],
        [cb("e", tokens["p1"], "🟡 Средний")],
        [cb("e", tokens["p0"], "⚪️ Низкий")],
        [cb("e", tokens["back"], "◀️ Назад")],
    ])


def stage_menu(stages: list[tuple[str, str]], back_token: str,
               app_url: str | None = None) -> dict[str, Any]:
    """Колонки канбана по одной в ряд: названия задаёт владелец проекта, они длинные.

    Список приходит с портала живьём, поэтому в чате видно ровно то же, что
    в Битриксе, — включая колонку, заведённую пять минут назад.
    """
    rows: Rows = [[cb("e", token, label[:60])] for token, label in stages]
    if app_url:
        rows.append([url_button("🧩 Открыть в приложении", app_url)])
    rows.append([cb("e", back_token, "◀️ Назад")])
    return inline(rows)


def people_menu(people: list[tuple[str, str]], back_token: str,
                app_url: str | None = None) -> dict[str, Any]:
    """Список людей по одному в ряд: имена длинные, в два столбца не читаются."""
    rows: Rows = [[cb("e", token, label[:60])] for token, label in people]
    if app_url:
        rows.append([url_button("🧩 Весь список — в приложении", app_url)])
    rows.append([cb("e", back_token, "◀️ Назад")])
    return inline(rows)


def confirm(token_yes: str, token_no: str, *, yes: str = "✅ Создать",
            no: str = "❌ Отмена") -> dict[str, Any]:
    return inline([[cb("c", token_yes, yes), cb("x", token_no, no)]])


# ------------------------------------------------------ кнопки под уведомлением
# Уведомление приходит само, без чьего-либо нажатия, поэтому владельца у кнопок
# нет: нажать может любой участник чата, а права режет Битрикс в момент действия —
# ровно так же, как у кнопок меню (docs/30-bot-spec.md §7.3).
NOTIFY_BUTTONS: dict[str, tuple[str, str]] = {
    "card":       ("t", "📋 Карточка"),
    "discussion": ("d", "💬 Обсуждение"),
    "start":      ("a", "▶️ В работу"),
    "renew":      ("a", "↩️ Вернуть в работу"),
    "edit":       ("e", "✏️ Изменить"),
    "deadline":   ("e", "⏰ Срок"),
    "stage":      ("e", "📂 Стадия"),
}


def notify_task(tokens: list[tuple[str, str]],
                portal_url: str | None = None) -> dict[str, Any] | None:
    """Действия под уведомлением: сами действия в ряд, ссылка на портал — отдельно.

    `tokens` — пары (вид кнопки, токен) в порядке показа. Неизвестный вид молча
    не пропускается: клавиатура собирается из кода, а не из данных, и опечатка в
    ней должна быть видна на тестах, а не в чате у клиента.
    """
    row = [cb(NOTIFY_BUTTONS[kind][0], token, NOTIFY_BUTTONS[kind][1])
           for kind, token in tokens]
    rows: Rows = [row] if row else []
    if portal_url:
        rows.append([url_button("🔗 Открыть в Битрикс24", portal_url)])
    return inline(rows) if rows else None
