"""Редактирование задачи: проверка полей, сроки и сверка записанного.

Отдельно от HTTP: те же функции вызывает бот кнопками, и ошибка здесь одинаково
испортит оба интерфейса.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from b24bot.bot import handlers
from b24bot.domain import tasks as service


class Portal:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params or {}))
        return {"task": self.task}


async def _no_echo(*_: object, **__: object) -> None:
    return None


# --------------------------------------------------------------- проверка полей
def test_deadline_requires_offset() -> None:
    with pytest.raises(service.Invalid) as exc:
        service.parse_deadline("2026-08-20T18:00:00")
    assert exc.value.field == "deadline"


def test_deadline_with_offset_normalised() -> None:
    assert service.parse_deadline("2026-08-20T18:00:00+03:00") == "2026-08-20T18:00:00+03:00"


def test_empty_deadline_means_clear() -> None:
    assert service.parse_deadline("") == ""


@pytest.mark.parametrize("value", [-1, 3, "высокий"])
def test_priority_bounds(value: object) -> None:
    with pytest.raises(service.Invalid):
        service.validate_patch({"priority": value})


def test_unknown_field_rejected() -> None:
    with pytest.raises(service.Invalid):
        service.validate_patch({"status": 5})


def test_empty_patch_rejected() -> None:
    with pytest.raises(service.Invalid):
        service.validate_patch({})


def test_description_is_escaped_for_bbcode() -> None:
    """И-6 без исключений: квадратные скобки Битрикс съедает как разметку."""
    patch = service.validate_patch({"description": "смотри [b]тут[/b]"})
    assert "[b]" not in patch["description"]
    assert "［b］" in patch["description"]


# ----------------------------------------------------------------- сроки
def test_offset_taken_from_portal_dates() -> None:
    """Часовой пояс берём из дат самого портала — у каждого теннанта он свой."""
    assert service.offset_of(None, "2026-08-11T23:03:41+03:00") == timedelta(hours=3)
    assert service.offset_of(None, "мусор") == timedelta(0)


def test_preset_deadline_is_local_evening_with_offset() -> None:
    now = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)
    value = service.preset_deadline("tomorrow", timedelta(hours=3), now=now)
    assert value == "2026-08-17T18:00:00+03:00"


def test_preset_clear_is_empty() -> None:
    assert service.preset_deadline("clear", timedelta(hours=3)) == ""


def test_preset_unknown_rejected() -> None:
    with pytest.raises(service.Invalid):
        service.preset_deadline("послезавтра", timedelta(0))


# ------------------------------------------------------------- запись и сверка
async def test_responsible_goes_through_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("b24bot.domain.events.suppress_echo", _no_echo)
    portal = Portal({"id": "100", "responsibleId": "9", "priority": "1"})
    task, missed = await service.apply_patch(portal, 1, 100, {"responsible_id": 9},
                                             actor_b24_user_id=7)
    assert ("tasks.task.delegate", {"taskId": 100, "userId": 9}) in portal.calls
    assert not any(m == "tasks.task.update" for m, _ in portal.calls)
    assert missed == []
    assert task["responsibleId"] == "9"


async def test_ignored_field_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Портал молча игнорирует то, что не смог применить (проверено записью)."""
    monkeypatch.setattr("b24bot.domain.events.suppress_echo", _no_echo)
    portal = Portal({"id": "100", "priority": "1"})
    _, missed = await service.apply_patch(portal, 1, 100, {"priority": 2},
                                          actor_b24_user_id=7)
    assert missed == ["приоритет"]


async def test_same_moment_in_other_notation_counts_as_applied(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Портал возвращает срок в своей записи — сравнивать надо моменты, не строки."""
    monkeypatch.setattr("b24bot.domain.events.suppress_echo", _no_echo)
    portal = Portal({"id": "100", "deadline": "2026-08-20T18:00:00+03:00"})
    _, missed = await service.apply_patch(
        portal, 1, 100, {"deadline": "2026-08-20T15:00:00+00:00"},
        actor_b24_user_id=7)
    assert missed == []


async def test_cleared_deadline_is_checked_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("b24bot.domain.events.suppress_echo", _no_echo)
    portal = Portal({"id": "100", "deadline": "2026-08-20T18:00:00+03:00"})
    _, missed = await service.apply_patch(portal, 1, 100, {"deadline": ""},
                                          actor_b24_user_id=7)
    assert missed == ["срок"]


# ------------------------------------------------------------- кнопки бота
def test_bot_deadline_button_uses_portal_timezone() -> None:
    task = {"createdDate": "2026-08-11T23:03:41+03:00"}
    patch = handlers._edit_patch("set_deadline", "tomorrow", task)
    assert patch is not None
    assert patch["deadline"].endswith("+03:00")
    assert patch["deadline"].endswith("T18:00:00+03:00")


def test_bot_priority_button_validated() -> None:
    assert handlers._edit_patch("set_priority", 2, {}) == {"priority": 2}


def test_bot_unknown_action_gives_nothing() -> None:
    assert handlers._edit_patch("set_something", 1, {}) is None
