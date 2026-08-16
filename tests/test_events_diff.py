"""Сравнение состояния задачи с кэшем. Событие Битрикса diff не содержит."""
from __future__ import annotations

from datetime import UTC, datetime

from b24bot.domain.events import DEFAULTS, _dt, fingerprint


def test_deadline_parsed_to_same_type_as_cache() -> None:
    """Живой баг: строка из портала против timestamptz из БД никогда не совпадали,
    и «изменён срок» улетало в чат на каждое событие."""
    from_portal = _dt("2026-08-16T19:00:00+03:00")
    from_cache = datetime(2026, 8, 16, 16, 0, tzinfo=UTC)
    assert from_portal == from_cache, "один и тот же момент времени обязан быть равен"


def test_dt_survives_garbage() -> None:
    assert _dt(None) is None
    assert _dt("") is None
    assert _dt("не дата") is None


def test_fingerprint_depends_on_all_three_parts() -> None:
    """Отпечаток обязан включать поле, НОВОЕ значение и автора.

    Схема без нового значения позволяла держать задачу «немой» в чате, повторяя
    безобидную правку раз в минуту.
    """
    base = fingerprint("STATUS", 5, 1)
    assert base != fingerprint("STATUS", 3, 1), "другое значение — другой отпечаток"
    assert base != fingerprint("STAGE", 5, 1), "другое поле — другой отпечаток"
    assert base != fingerprint("STATUS", 5, 2), "другой автор — другой отпечаток"
    assert base == fingerprint("STATUS", 5, 1)


def test_task_created_is_off_by_default() -> None:
    """Первое событие о незнакомой задаче наполняет кэш, а не шумит в чат.

    Иначе при подключении портала весь накопленный хвост хлынет в чат разом.
    """
    assert DEFAULTS["task.created"] is False
    assert DEFAULTS["task.status_changed"] is True
    assert DEFAULTS["task.deleted"] is True
