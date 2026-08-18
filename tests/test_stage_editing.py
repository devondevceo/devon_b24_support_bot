"""Стадия канбана: видно её и можно ли её подвинуть.

Стадия и статус на портале независимы (docs/00-portal-facts.md §3.2), и в чате
работу меряют именно стадией — «Новые», «Выполняются», «Сделаны». До этой правки
её нельзя было ни увидеть в уведомлении, ни сдвинуть кнопкой: карточка бота
показывала вместо названия голый номер колонки, потому что своего названия портал
в задаче не отдаёт вовсе.
"""
from __future__ import annotations

import re
from pathlib import Path

from b24bot.bot import keyboards, views
from b24bot.bot.handlers import _edit_patch, _edit_summary
from b24bot.domain.events import NOTIFY_ACTIONS, NOTIFY_PAYLOAD, stage_note

HANDLERS = (Path(__file__).resolve().parents[1]
            / "src" / "b24bot" / "bot" / "handlers.py").read_text(encoding="utf-8")

TITLES = {333: "Новые", 335: "Выполняются", 337: "Сделаны"}


def test_two_meanings_stay_two_strings() -> None:
    """«Не разложена по канбану» и «мы не знаем такой стадии» — разные вещи.

    Пока смыслы жили в одной строке, второй не видел никто.
    """
    assert views.stage_label(0, TITLES) == views.OUTSIDE_KANBAN
    assert views.stage_label(None, TITLES) == views.OUTSIDE_KANBAN
    assert views.stage_label(335, TITLES) == "Выполняются"
    assert views.stage_label(999, TITLES) == views.UNKNOWN_STAGE
    assert views.stage_label("337", TITLES) == "Сделаны", "портал шлёт числа строками"


def test_card_shows_the_name_not_the_number() -> None:
    """Раньше в карточке стояло «Стадия: 337» — номер колонки человеку ничего не даёт."""
    task = {"id": 233, "title": "Лид-форма", "status": 3, "stageId": 337}
    card = views.render_card(task, _project(), stage_title="Сделаны")
    assert "Стадия: Сделаны" in card
    assert "Стадия: 337" not in card


def test_card_without_a_reference_says_nothing_instead_of_lying() -> None:
    task = {"id": 233, "title": "Лид-форма", "status": 3, "stageId": 337}
    assert "Стадия:" not in views.render_card(task, _project())


def test_stage_line_of_a_notification() -> None:
    """Задача вне канбана — это состояние, а не пропуск: строка обязана быть."""
    assert stage_note("Сделаны") == "Стадия: Сделаны"
    assert stage_note("") == f"Стадия: {views.OUTSIDE_KANBAN}"
    assert stage_note("<b>взлом</b>") == "Стадия: &lt;b&gt;взлом&lt;/b&gt;", "И-6"


def test_notifications_carry_the_stage_button() -> None:
    """Сдвинуть колонку — самый частый ответ на уведомление о ходе работы."""
    assert "stage" in NOTIFY_ACTIONS["task.stage_changed"]
    assert "stage" in NOTIFY_ACTIONS["task.status_changed"]
    assert NOTIFY_PAYLOAD["stage"] == {"act": "stage_menu"}
    assert keyboards.NOTIFY_BUTTONS["stage"][0] == "e", "меню правки живёт в ns e"


def test_edit_menu_offers_the_stage() -> None:
    tokens = {"deadline_menu": "d", "assignee_menu": "a", "priority_menu": "p",
              "stage_menu": "s", "back": "b"}
    labels = [b["text"] for row in keyboards.edit_menu(tokens)["inline_keyboard"]
              for b in row]
    assert "📂 Стадия" in labels


def test_stage_menu_marks_where_we_are_and_leads_back() -> None:
    markup = keyboards.stage_menu([("t1", "✅ Выполняются"), ("t2", "Сделаны")], "back")
    rows = markup["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["✅ Выполняются"]
    assert rows[-1][0]["callback_data"] == "e:back"


def test_button_press_turns_into_a_stage_patch() -> None:
    assert _edit_patch("set_stage", 337, {}) == {"stage_id": 337}
    assert _edit_patch("set_stage", 0, {}) == {"stage_id": 0}, "вернуть вне канбана можно"
    assert _edit_summary("set_stage", {}, {"stage_id": 337}) == "стадия"


def test_stage_menu_is_actually_routed() -> None:
    """Кнопка `stage_menu` обязана разбираться в `_edit`, иначе она молчит."""
    body = HANDLERS[HANDLERS.index("async def _edit("):]
    body = body[:body.index("\ndef _edit_patch")]
    for act in ("stage_menu", "set_stage"):
        assert re.search(rf'"{act}"', body), f"{act} не разбирается в _edit"


def _project() -> views.ProjectRef:
    return views.ProjectRef(1, 33, "Devon SD BOT", "Линия Жизни")
