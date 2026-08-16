"""Извлечение заголовка и описания. Без LLM, по правилам docs/30-bot-spec.md §1.2."""
from __future__ import annotations

from b24bot.bot.handlers import _command, _text_of
from b24bot.bot.task_create import extract, message_link


def draft(text: str, **kw):
    return extract(text, author="Иван Иванов (@ivan)", chat_title="Поддержка",
                   message_link=None, idem_key="tgsrc-1-1", **kw)


def test_command_is_stripped_from_title() -> None:
    """Живой баг: в заголовок задачи #209 уехало «/task ...» целиком."""
    msg = {"text": "/task Не открывается форма записи", "message_id": 11}
    name, arg = _command(msg["text"], "devon_sd_bot")
    assert name == "task"
    source = {**msg, "text": arg, "caption": None}
    assert _text_of(source) == "Не открывается форма записи"
    assert draft(_text_of(source)).title == "Не открывается форма записи"


def test_command_addressed_to_another_bot_ignored() -> None:
    assert _command("/task@other_bot текст", "devon_sd_bot") is None
    assert _command("/task@devon_sd_bot текст", "devon_sd_bot") == ("task", "текст")


def test_title_is_first_line() -> None:
    d = draft("Не работает форма\nПодробности: при нажатии ничего")
    assert d.title == "Не работает форма"
    assert "Подробности" in d.description


def test_long_title_cut_by_word_boundary() -> None:
    d = draft("а" * 20 + " " + "б" * 100)
    assert d.title.endswith("…")
    assert len(d.title) <= 82


def test_short_text_gets_fallback_title() -> None:
    assert draft("ой").title == "Обращение из Telegram"


def test_source_block_is_escaped() -> None:
    """Название чата и имя автора задаёт кто угодно, а Битрикс ест скобки как BBCode."""
    d = extract("текст", author="[b]Админ[/b]",
                chat_title="[url=https://evil]клик[/url]",
                message_link=None, idem_key="k")
    assert "[url=" not in d.description
    assert "[/b]Админ" not in d.description
    assert "— Источник —" in d.description


def test_message_link_only_for_supergroups() -> None:
    assert message_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"
    assert message_link(-5075800320, 42) is None
    assert message_link(12345, 42) is None


# ------------------------------------------------------ вложенность и кнопки
def test_hierarchy_puts_subtasks_under_parent() -> None:
    from b24bot.bot.views import order_by_hierarchy

    tasks = [
        {"id": "1", "parentId": None},
        {"id": "2", "parentId": "1"},
        {"id": "3", "parentId": None},
        {"id": "4", "parentId": "2"},
    ]
    ordered = order_by_hierarchy(tasks)
    assert [(t["id"], d) for t, d in ordered] == [
        ("1", 0), ("2", 1), ("4", 2), ("3", 0)]


def test_orphan_subtask_stays_visible() -> None:
    """Родителя может не быть в выборке — он закрыт или в другом проекте.
    Терять подзадачу нельзя."""
    from b24bot.bot.views import order_by_hierarchy

    ordered = order_by_hierarchy([{"id": "9", "parentId": "777"}])
    assert [(t["id"], d) for t, d in ordered] == [("9", 0)]


def test_button_order_matches_text_order() -> None:
    """Номер кнопки обязан указывать на ту же задачу, что и строка списка."""
    from b24bot.bot.views import flatten_for_buttons, order_by_hierarchy

    tasks = [{"id": "1", "parentId": None}, {"id": "2", "parentId": "1"},
             {"id": "3", "parentId": None}]
    assert [t["id"] for t in flatten_for_buttons(tasks)] == \
           [t["id"] for t, _ in order_by_hierarchy(tasks)]
