"""Кнопки под уведомлением и ссылка на задачу в тексте.

Уведомление — единственное сообщение бота, которое никто не вызывал нажатием:
его шлёт воркер из очереди. Отсюда два требования, которые здесь и проверяются.

1. **Кнопка обязана вести туда, куда обещает подпись.** Набор кнопок собирается
   по коду события таблицей `NOTIFY_ACTIONS`, а не по месту, и опечатка в ней
   должна валить тест, а не всплывать «диалогом устаревшим» в чате у клиента.
2. **Нажатие не имеет права стереть само уведомление.** Списки и карточки живут
   в одном сообщении и редактируются на месте; уведомление так редактировать
   нельзя — это запись о событии, и заменить её карточкой значит стереть из
   истории чата то, о чём вообще было сообщение.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from b24bot.bot import keyboards
from b24bot.bot.handlers import Reply, _keep_source
from b24bot.domain.events import DEFAULTS, NOTIFY_ACTIONS, NOTIFY_PAYLOAD

HANDLERS = (Path(__file__).resolve().parents[1]
            / "src" / "b24bot" / "bot" / "handlers.py").read_text(encoding="utf-8")


def test_every_notification_code_has_a_button_set() -> None:
    """Новый тип уведомления без записи в таблице остался бы без кнопок молча."""
    missing = sorted(set(DEFAULTS) - set(NOTIFY_ACTIONS))
    assert not missing, f"уведомления без набора кнопок: {missing}"


def test_deleted_task_has_no_buttons() -> None:
    """Открывать, комментировать и менять уже нечего: кнопка вела бы в никуда."""
    assert NOTIFY_ACTIONS["task.deleted"] == ()


def test_button_kinds_are_known_everywhere() -> None:
    """Вид кнопки описан в трёх местах сразу — подпись, полезная нагрузка, набор."""
    used = {kind for kinds in NOTIFY_ACTIONS.values() for kind in kinds}
    assert used <= set(keyboards.NOTIFY_BUTTONS), "нет подписи для вида кнопки"
    assert used <= set(NOTIFY_PAYLOAD), "нет полезной нагрузки для вида кнопки"


def test_every_button_namespace_is_routed() -> None:
    """Тот же страж, что у слеш-команд: кнопка обязана что-то делать.

    Пространство имён из клавиатуры, которого нет в `on_callback`, — это молчащая
    кнопка: нажатие проглатывается, и бот выглядит сломанным.
    """
    routed = set(re.findall(r'ns == "(\w+)"', HANDLERS))
    namespaces = {ns for ns, _ in keyboards.NOTIFY_BUTTONS.values()}
    assert namespaces <= routed, f"кнопки без обработчика: {sorted(namespaces - routed)}"


def test_actions_match_the_meaning_of_the_event() -> None:
    """Смысл кнопки задаёт событие, иначе набор превращается в универсальный."""
    assert "start" in NOTIFY_ACTIONS["task.created"], "на новую задачу отвечают «беру»"
    assert NOTIFY_ACTIONS["task.comment_added"][0] == "discussion", \
        "на комментарий первым делом читают обсуждение"
    assert "renew" in NOTIFY_ACTIONS["task.completed"], \
        "закрытую не туда возвращают в работу"
    assert "deadline" in NOTIFY_ACTIONS["task.deadline_changed"]


def test_action_payload_carries_the_method_name() -> None:
    """`act` уезжает прямо в обработчик действия: опечатка = «диалог устарел»."""
    assert NOTIFY_PAYLOAD["start"] == {"act": "start"}
    assert NOTIFY_PAYLOAD["renew"] == {"act": "renew"}
    assert NOTIFY_PAYLOAD["card"] == {}


def test_bot_knows_every_action_the_buttons_offer() -> None:
    """Действие из кнопки обязано быть в наборе `_task_action`, иначе отказ."""
    acts = {payload["act"] for kind, payload in NOTIFY_PAYLOAD.items()
            if keyboards.NOTIFY_BUTTONS[kind][0] == "a"}
    handled = set(re.findall(r'"(\w+)": "tasks\.task\.\w+"', HANDLERS))
    assert acts <= handled, f"кнопка есть, действия нет: {sorted(acts - handled)}"


def test_keyboard_puts_portal_link_on_its_own_row() -> None:
    markup = keyboards.notify_task([("card", "tok1"), ("discussion", "tok2")],
                                   "https://portal.example/task/1/")
    assert markup is not None
    rows = markup["inline_keyboard"]
    assert [b["callback_data"] for b in rows[0]] == ["t:tok1", "d:tok2"]
    assert rows[1][0]["url"] == "https://portal.example/task/1/"


def test_keyboard_without_anything_to_show_is_absent() -> None:
    """Пустая клавиатура — это отсутствие клавиатуры, а не пустой прямоугольник."""
    assert keyboards.notify_task([], None) is None


def test_unknown_button_kind_fails_loudly() -> None:
    """Клавиатура собирается из кода, а не из данных: молчать здесь нельзя."""
    with pytest.raises(KeyError):
        keyboards.notify_task([("такого-нет", "tok")], None)


async def test_markup_is_built_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Весь путь от кода события до клавиатуры, без базы: токены подменены.

    Проверяется то, ради чего всё и затевалось: под уведомлением о комментарии
    появляются рабочие кнопки, а не подпись «см. задачу #233».
    """
    from b24bot.domain import events

    issued: list[dict[str, object]] = []

    async def fake_issue(kind: str, **kw: object) -> str:
        issued.append({"kind": kind, **kw})
        return f"tok{len(issued)}"

    monkeypatch.setattr(events, "issue_token", fake_issue)
    markup = await events.notify_markup(1, 5, 233, "task.comment_added",
                                        "https://p.example/tasks/task/view/233/")

    assert markup is not None
    rows = markup["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["💬 Обсуждение", "📋 Карточка"]
    assert [b["callback_data"] for b in rows[0]] == ["d:tok1", "t:tok2"]
    assert rows[1][0]["url"] == "https://p.example/tasks/task/view/233/"

    # Токен привязан к чату, живёт долго и не одноразовый: уведомление лежит
    # в истории, а владельца у кнопки нет — нажать может любой участник чата.
    for token in issued:
        assert token["chat_ref"] == 5
        assert token["single_use"] is False
        assert token["ttl"] == events.NOTIFY_TOKEN_TTL
        assert "owner_tg_id" not in token
        assert token["payload"] == {"task_id": 233, "notify": True}


async def test_deleted_task_notification_gets_no_keyboard(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from b24bot.domain import events

    async def fail(*_a: object, **_kw: object) -> str:
        raise AssertionError("токен для несуществующей кнопки")

    monkeypatch.setattr(events, "issue_token", fail)
    assert await events.notify_markup(1, 5, 233, "task.deleted", None) is None
    # И ссылки на портал тоже: открывать уже нечего.
    assert await events.notify_markup(1, 5, 233, "task.deleted",
                                      "https://p.example/x/") is None


def test_worker_reads_the_keyboard_in_both_shapes() -> None:
    """asyncpg отдаёт JSONB строкой, пока не поставлен кодек: держим оба случая."""
    from b24bot.worker.main import _markup

    keyboard = {"inline_keyboard": [[{"text": "📋 Карточка", "callback_data": "t:x"}]]}
    assert _markup({"id": 1, "markup": keyboard}) == keyboard
    assert _markup({"id": 1, "markup": '{"inline_keyboard": []}'}) == {
        "inline_keyboard": []}
    assert _markup({"id": 1, "markup": None}) is None


def test_broken_keyboard_does_not_hold_up_the_message() -> None:
    """Текст уведомления важнее кнопок: сломанная разметка не повод молчать."""
    from b24bot.worker.main import _markup

    assert _markup({"id": 1, "markup": "{не json"}) is None
    assert _markup({"id": 1, "markup": "[1,2]"}) is None


def test_press_under_notification_answers_with_a_new_message() -> None:
    """Иначе карточка затрёт уведомление, и о чём оно было — не восстановить."""
    reply = _keep_source({"task_id": 1, "notify": True}, Reply("карточка", edit=True))
    assert reply.edit is False


def test_press_under_list_still_edits_in_place() -> None:
    """Признака `notify` нет — значит нажали под списком или карточкой."""
    reply = _keep_source({"task_id": 1}, Reply("карточка", edit=True))
    assert reply.edit is True
