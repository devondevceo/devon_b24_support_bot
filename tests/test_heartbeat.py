"""Healthcheck обязан отличать живой процесс от мёртвого.

До этого воркер наследовал из образа curl на порт, которого у него нет, и вечно
числился unhealthy, а бот проверялся заглушкой, которая проходила всегда.
Оба теста ниже — про то, что новый healthcheck не повторяет ни одну из ошибок.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from b24bot.core import heartbeat


@pytest.fixture(autouse=True)
def tmp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat, "DIR", tmp_path / "beats")


def test_fresh_beat_passes() -> None:
    heartbeat.beat("worker")
    assert heartbeat.main(["_", "worker", "60"]) == 0


def test_missing_beat_fails() -> None:
    """Процесс не начал работать — это не «здоров»."""
    assert heartbeat.main(["_", "worker", "60"]) == 1


def test_stale_beat_fails() -> None:
    """Зависший цикл обязан валить healthcheck, а не молчать."""
    heartbeat.beat("worker")
    heartbeat.path_for("worker").write_text(str(time.time() - 3600), encoding="ascii")
    assert heartbeat.main(["_", "worker", "60"]) == 1


def test_processes_do_not_share_a_beat() -> None:
    """Один образ, три контейнера: пульс бота не должен лечить воркера."""
    heartbeat.beat("bot")
    assert heartbeat.main(["_", "bot", "60"]) == 0
    assert heartbeat.main(["_", "worker", "60"]) == 1


def test_unwritable_dir_does_not_break_the_loop() -> None:
    """Пульс — диагностика. Она не имеет права уронить рабочий цикл."""
    heartbeat.DIR = Path("/proc/nonexistent/beats")  # type: ignore[misc]
    heartbeat.beat("worker")
