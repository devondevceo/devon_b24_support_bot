"""Сторож приёма апдейтов: зависший long polling обязан быть видим и починен.

Живая авария 08.09.2026, из-за которой этот файл и появился. Бот одиннадцать
дней не забирал сообщения: `getUpdates` завис так, что таймаут HTTP-клиента не
сработал; задача поллера при этом не падала и не завершалась, супервизор исправно
бился пульсом за свой собственный оборот, а контейнер числился `healthy`. Снаружи
это выглядело как «бот перестал создавать задачи по реплаю» — то есть как баг
продукта, хотя ни одна строка бизнес-логики не выполнялась вовсе.

Отсюда два правила, которые здесь и проверяются:

1. **Свой предел на заход в getUpdates.** Таймаут библиотеки — обещание
   библиотеки; наш `asyncio.timeout` обойти нечем.
2. **Пульс отвечает за опрос, а не за супервизора.** Здоровье процесса — это
   «апдейты забираются», а не «цикл, который смотрит на список ботов, жив».
"""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from b24bot.bot import poller as poller_mod
from b24bot.bot.poller import BotPoller, PollerRegistry
from b24bot.tg import api as tg


def _poller(idle: float = 0.0) -> BotPoller:
    p = BotPoller(1, 1, 100, "devon_sd_bot", "123:secret", offset=10)
    p._polled_at = time.monotonic() - idle
    return p


def test_fresh_poller_is_not_stalled() -> None:
    assert _poller().idle_for() < 1.0


def test_hard_limit_is_longer_than_the_long_poll_itself() -> None:
    """Предел обязан превышать штатное ожидание, иначе он рубит здоровый запрос.

    Та же ошибка с другой стороны уже была в HTTP-клиенте: таймаут короче
    `getUpdates` превращал long polling в серию таймаутов.
    """
    assert poller_mod.POLL_HARD_LIMIT > poller_mod.POLL_TIMEOUT
    assert poller_mod.STALL_AFTER > poller_mod.POLL_HARD_LIMIT


@pytest.mark.asyncio
async def test_registry_pulse_stops_when_polling_stalls() -> None:
    """Пульс — свойство опроса. Иначе healthcheck покрывает тишину.

    Ровно это и случилось: пульс ставил супервизор за свой оборот, и одиннадцать
    дней молчания выглядели как здоровый контейнер.
    """
    reg = PollerRegistry()
    reg._pollers[1] = _poller()
    assert reg.polling() is True

    reg._pollers[1] = _poller(idle=poller_mod.STALL_AFTER + 1)
    assert reg.polling() is False


@pytest.mark.asyncio
async def test_empty_registry_counts_as_healthy() -> None:
    """Ботов нет — опрашивать нечего, и краснеть тоже не за что."""
    assert PollerRegistry().polling() is True


@pytest.mark.asyncio
async def test_hung_long_poll_does_not_wedge_the_loop(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Зависший запрос обрывается своим пределом, и цикл идёт дальше.

    До правки этот тест висел бы вечно — ровно как боевой поллер.
    """
    monkeypatch.setattr(poller_mod, "POLL_HARD_LIMIT", 0.05)
    monkeypatch.setattr(poller_mod, "ERROR_SLEEP", 0.01)

    attempts = 0

    async def hangs(*args: object, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(3600)

    async def no_commands(*args: object, **kwargs: object) -> object:
        return True

    monkeypatch.setattr(tg, "call", hangs)
    monkeypatch.setattr(tg, "delete_webhook", no_commands)
    monkeypatch.setattr(tg, "set_my_commands", no_commands)

    p = _poller()
    task = asyncio.create_task(p.run())
    await asyncio.sleep(0.3)
    p.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert attempts > 1, "после обрыва цикл обязан пробовать снова, а не стоять"
    # Оборот засчитан: значит сторож не сочтёт живой цикл зависшим только из-за
    # того, что Telegram молчит.
    assert p.idle_for() < poller_mod.STALL_AFTER


@pytest.mark.asyncio
async def test_repeated_timeouts_switch_to_short_polling(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Длинный опрос не доживает до ответа — переходим на короткий.

    Это и была живая авария: 25-секундное удержание через прокси умирало
    ReadTimeout, а такой же запрос с нулевым ожиданием отвечал за доли секунды.
    Бот при этом молчал одиннадцать дней.
    """
    p = _poller()
    assert p._poll_timeout() == poller_mod.POLL_TIMEOUT

    for _ in range(poller_mod.FALLBACK_AFTER):
        p._on_timeout()
    assert p._short_poll is True
    assert p._poll_timeout() == 0.0, "короткий опрос просит Telegram не ждать"


@pytest.mark.asyncio
async def test_long_polling_is_retried_by_the_clock_not_by_luck() -> None:
    """Короткий опрос успешен всегда — по числу удач мы бы не вернулись никогда."""
    p = _poller()
    for _ in range(poller_mod.FALLBACK_AFTER):
        p._on_timeout()
    p._on_success()
    assert p._poll_timeout() == 0.0, "успех короткого опроса сам по себе не повод"

    p._long_retry_at = time.monotonic() - 1
    assert p._poll_timeout() == poller_mod.POLL_TIMEOUT
    assert p._short_poll is False


@pytest.mark.asyncio
async def test_timeout_counts_as_a_completed_lap() -> None:
    """Сеть молчит — цикл всё равно жив, и сторож не должен его переподнимать."""
    p = _poller(idle=poller_mod.STALL_AFTER + 5)
    assert PollerRegistry().polling() is True  # пустой реестр
    p._on_timeout()
    assert p.idle_for() < 1.0
