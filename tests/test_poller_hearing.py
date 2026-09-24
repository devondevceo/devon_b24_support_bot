"""Приём апдейтов: здоровым считается бот, которому Telegram отдаёт сообщения.

Живые аварии, из-за которых этот файл существует. 08.09.2026 бот одиннадцать дней
не забирал сообщения при `healthy`-контейнере: пульс ставил супервизор. После той
правки пульс ставился за оборот цикла — и 23.09.2026 бот восемь часов (13:38–21:30
UTC) не видел ни одного апдейта, а контейнер снова был `healthy`, `/health` — `ok`,
экран Битрикса — «Интеграция работает». Прокси резал крупный апдейт в голове
очереди, каждый `getUpdates` кончался таймаутом, и оборотом засчитывался и он: цикл
не висел, он честно крутился вхолостую. В очереди Telegram тем временем стояло 19.

Отсюда правила, которые здесь проверяются:

1. **Пульс — по ответу Telegram, а не по обороту цикла.**
2. **Очередь глазами Telegram** (`getWebhookInfo`): не пустеет при стоящем offset —
   бот не забирает сообщения, даже если каждый `getUpdates` отвечает.
3. **Вторая копия бота** видна по повторяющемуся 409 «другой getUpdates».
4. **Состояние приёма переживает перезапуск цикла**, иначе каждый перезапуск давал
   бы пульсу новую отсрочку.
5. **Отчёт в `tg_bots` раз в минуту**, `heard_at` — только по настоящему ответу, и
   ни одна вспышка сети не долетает до экрана.
6. **409 из-за вебхука лечится на месте**, чужой вебхук останавливает бота.
7. **Новый токен доезжает до опроса.**

Большая часть тестов гоняет настоящий `BotPoller.run()` против подделки Bot API
по часам: время двигает не настоящий сон, а сама подделка — заход, который висит до
своего предела, стоит ровно этот предел. Поэтому восемь часов аварии проходят за
миллисекунды, и каждый тест говорит, в какую минуту что должно было случиться.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from b24bot.bot import dispatch, reception
from b24bot.bot import poller as poller_mod
from b24bot.bot.poller import BotPoller, PollerRegistry
from b24bot.bot.reception import Problem, Reception
from b24bot.core.config import get_settings
from b24bot.tg import api as tg

WEBHOOK_409 = ("Conflict: can't use getUpdates method while webhook is active; "
               "use deleteWebhook to delete the webhook first")
OTHER_409 = ("Conflict: terminated by other getUpdates request; make sure that only "
             "one bot instance is running")
START = 10_000.0


# ------------------------------------------------------------------ стенд
class Clock:
    """Монотонные часы поллера. Их двигает подделка Telegram и сон цикла."""

    def __init__(self) -> None:
        self.now = START
        self.after_sleep: list[Callable[[], None]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in self.after_sleep:
            hook()
        await asyncio.sleep(0)


def _update(update_id: int) -> dict[str, Any]:
    return {"update_id": update_id,
            "message": {"message_id": 1, "chat": {"id": -100, "type": "supergroup"}}}


class Telegram:
    """Bot API по часам: что каждый метод отвечает в данную минуту.

    `getUpdates` подтверждает всё ниже offset и отдаёт очередь; пустая очередь
    держит длинный опрос свои 25 секунд. `fail` — чем кончается каждый заход;
    таймаут при этом стоит ровно предел захода, как на проде. `withhold` — Telegram
    отвечает пустым списком, хотя очередь не пуста: так выглядит сбой, при котором
    каждый заход успешен, а сообщения не приходят. `getWebhookInfo` отвечает длиной
    очереди — её-то и сверяет поллер.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.queue: list[dict[str, Any]] = []
        self.fail: BaseException | None = None
        self.withhold = False
        self.trickle = False          # к каждому заходу — новый апдейт из чата
        self.next_id = 500
        self.info_fail: BaseException | None = None
        self.webhook_url = ""
        self.calls: list[tuple[float, str]] = []

    def enqueue(self, n: int) -> None:
        for _ in range(n):
            self.queue.append(_update(self.next_id))
            self.next_id += 1

    async def call(self, token: str, method: str, params: dict[str, Any] | None = None,
                   **kwargs: object) -> Any:
        params = params or {}
        self.calls.append((self.clock.now - START, method))
        await asyncio.sleep(0)
        if method == "getUpdates":
            return self._get_updates(params)
        if method == "getWebhookInfo":
            self.clock.now += 0.2
            if self.info_fail is not None:
                raise self.info_fail
            return {"url": self.webhook_url, "has_custom_certificate": False,
                    "pending_update_count": len(self.queue)}
        if method == "deleteWebhook":
            self.webhook_url = ""
        return True

    def _get_updates(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self.webhook_url:
            self.clock.now += 0.2
            raise tg.TelegramError(409, WEBHOOK_409)
        if self.fail is not None:
            if reception.is_timeout(self.fail):
                self.clock.now += tg.deadline("getUpdates", params) + poller_mod.HARD_MARGIN
            else:
                self.clock.now += 0.3
            raise self.fail
        offset = int(params.get("offset") or 0)
        self.queue = [u for u in self.queue if u["update_id"] >= offset]
        if self.trickle:
            self.enqueue(1)
        if self.queue and not self.withhold:
            self.clock.now += 0.2
            return list(self.queue)
        self.clock.now += float(params.get("timeout") or 0) or 0.2
        return []

    def count(self, method: str) -> int:
        return sum(1 for _, m in self.calls if m == method)


@dataclass
class Sim:
    clock: Clock
    telegram: Telegram
    poller: BotPoller
    registry: PollerRegistry
    writes: list[tuple[float, str, tuple[object, ...]]] = field(default_factory=list)
    pulse: list[tuple[float, bool]] = field(default_factory=list)
    processed: list[int] = field(default_factory=list)

    @property
    def t(self) -> float:
        """Секунды с начала симуляции."""
        return self.clock.now - START

    def reports(self) -> list[tuple[float, tuple[object, ...]]]:
        """Отчёты о приёме: (когда, (heard_ago, poll_error, queue_pending))."""
        return [(at, args) for at, sql, args in self.writes
                if sql == poller_mod._SQL_RECEPTION]

    def last_report(self) -> tuple[object, ...]:
        return self.reports()[-1][1]

    def first_red(self) -> float | None:
        return next((t for t, ok in self.pulse if not ok), None)


def _build(monkeypatch: pytest.MonkeyPatch, poller: BotPoller | None = None) -> Sim:
    clock = Clock()
    telegram = Telegram(clock)
    monkeypatch.setattr(poller_mod, "clock", clock)
    monkeypatch.setattr(poller_mod, "_sleep", clock.sleep)
    monkeypatch.setattr(tg, "call", telegram.call)
    p = poller or BotPoller(1, 1, 100, "devon_sd_bot", "123:secret", offset=499)
    reg = PollerRegistry()
    reg._pollers[1] = p
    sim = Sim(clock, telegram, p, reg)

    async def write_state(self: BotPoller, sql: str, *args: object) -> bool:
        sim.writes.append((clock.now - START, sql, args))
        return True

    async def save_offset(self: BotPoller) -> None:
        return None

    async def handle(bot_ref: int, tenant_id: int, update: dict[str, Any]) -> None:
        sim.processed.append(int(update["update_id"]))

    async def route(bot_ref: int, update: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(BotPoller, "_write_state", write_state)
    monkeypatch.setattr(BotPoller, "_save_offset", save_offset)
    monkeypatch.setattr(dispatch, "handle", handle)
    monkeypatch.setattr(dispatch, "route", route)
    # Пульс снимается так же, как в `run_forever`: по реестру, между оборотами.
    clock.after_sleep.append(lambda: sim.pulse.append((sim.t, reg.hearing())))
    return sim


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Sim:
    return _build(monkeypatch)


@contextlib.asynccontextmanager
async def running(sim: Sim) -> AsyncIterator[Callable[[float], Awaitable[None]]]:
    """Настоящий `run()` поллера; `go(t)` — дать ему прожить до секунды `t`."""
    task = asyncio.create_task(sim.poller.run())

    async def go(until: float) -> None:
        for _ in range(200_000):
            if sim.t >= until or task.done():
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("симуляция не двигает часы")
        if task.done():
            task.result()  # исключение внутри цикла — провал теста, а не тишина

    try:
        yield go
    finally:
        sim.poller.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _long_limit() -> float:
    return (tg.deadline("getUpdates", {"timeout": poller_mod.POLL_TIMEOUT})
            + poller_mod.HARD_MARGIN)


# ------------------------------------------------------ правило 1: пульс
async def test_replay_of_23_09_turns_the_pulse_red(sim: Sim) -> None:
    """Восемь часов 23.09 в миниатюре: каждый заход — таймаут, в очереди 19.

    Старый сторож молчит — цикл не висит, обороты идут. Пульс обязан погаснуть,
    как только ответов нет дольше DEAF_AFTER, а экран — узнать причину.
    """
    sim.telegram.enqueue(19)
    sim.telegram.fail = TimeoutError()
    async with running(sim) as go:
        await go(900)

    red = sim.first_red()
    assert red is not None, "восемь часов без ответа — а пульс зелёный"
    assert reception.DEAF_AFTER < red < reception.DEAF_AFTER + _long_limit() + 30
    assert all(ok for t, ok in sim.pulse if t < reception.DEAF_AFTER), \
        "свежему процессу положена отсрочка: выкатка не начинается с красного"
    assert not any(ok for t, ok in sim.pulse if t > red), "пульс не мигает"
    assert sim.poller.idle_for() < poller_mod.STALL_AFTER, "сторож зависания тут молчит"

    heard_ago, reason, pending = sim.last_report()
    assert heard_ago is None, "Telegram ни разу не ответил — heard_at не трогаем"
    assert reason == reception.describe_failure(TimeoutError())
    assert pending == 19, "очередь видна и тогда, когда getUpdates не проходит"


async def test_answer_restores_the_pulse_and_clears_the_reason(sim: Sim) -> None:
    sim.telegram.fail = TimeoutError()
    async with running(sim) as go:
        await go(600)
        assert sim.registry.hearing() is False
        sim.telegram.fail = None
        await go(900)

    assert sim.registry.hearing() is True
    heard_ago, reason, _ = sim.last_report()
    assert reason is None, "ответ Telegram снимает причину"
    assert heard_ago is not None and heard_ago < poller_mod.CHECK_EVERY + 30


async def test_short_blip_never_reaches_the_screen(sim: Sim) -> None:
    """Пара таймаутов подряд — норма сети, а не сбой: ни красного, ни причины."""
    sim.telegram.fail = TimeoutError()
    async with running(sim) as go:
        await go(100)
        sim.telegram.fail = None
        await go(900)

    assert all(ok for _, ok in sim.pulse)
    assert all(args[1] is None for _, args in sim.reports())


def test_fresh_poller_is_heard() -> None:
    assert PollerRegistry().hearing() is True, "ботов нет — краснеть не за что"
    reg = PollerRegistry()
    reg._pollers[1] = BotPoller(1, 1, 100, "devon_sd_bot", "123:secret", offset=10)
    assert reg.hearing() is True


def test_deafness_outlasts_the_short_poll_fallback() -> None:
    """Сеть, где длинный опрос не живёт, а короткий живёт, — не глухота.

    До отступления на короткий опрос проходит FALLBACK_AFTER заходов по пределу
    захода плюс паузы; предел глухоты обязан быть длиннее, иначе healthcheck
    краснел бы на сети, где бот работает.
    """
    fallback = poller_mod.FALLBACK_AFTER * (_long_limit() + poller_mod.ERROR_SLEEP)
    assert fallback < reception.DEAF_AFTER


async def test_network_where_only_short_polls_live_is_not_deaf(sim: Sim) -> None:
    """Длинный опрос умирает, короткий отвечает — бот работает, пульс зелёный."""
    real = sim.telegram._get_updates

    def long_dies(params: dict[str, Any]) -> list[dict[str, Any]]:
        if params.get("timeout"):
            sim.clock.now += tg.deadline("getUpdates", params) + poller_mod.HARD_MARGIN
            raise TimeoutError()
        return real(params)

    sim.telegram._get_updates = long_dies  # type: ignore[method-assign]
    async with running(sim) as go:
        await go(1800)

    assert sim.poller._short_poll is True
    assert all(ok for _, ok in sim.pulse), "каждые 15 минут проба длинного — не авария"


# --------------------------------------------- правило 2: очередь Telegram
async def test_stuck_queue_is_seen_even_when_every_getupdates_answers(sim: Sim) -> None:
    """Самый тихий отказ: Telegram отвечает на каждый заход, а очередь стоит.

    Ни таймаута, ни ошибки, ни строчки в логе на заход — видно его только по
    очереди глазами самого Telegram.
    """
    sim.telegram.enqueue(19)
    sim.telegram.withhold = True
    async with running(sim) as go:
        await go(900)

    assert sim.poller.deaf_for() < 60, "Telegram отвечает"
    red = sim.first_red()
    assert red is not None, "19 сообщений стоят пятнадцать минут — а пульс зелёный"
    assert red > reception.QUEUE_STUCK_AFTER
    _, reason, pending = sim.last_report()
    assert pending == 19
    assert isinstance(reason, str) and "19" in reason


async def test_busy_chat_is_not_a_stuck_queue(sim: Sim) -> None:
    """Очередь не пуста при каждой проверке, но offset двигается — это работа."""
    sim.telegram.trickle = True
    async with running(sim) as go:
        await go(1800)

    assert len(sim.processed) > 50
    assert all(ok for _, ok in sim.pulse)
    pendings = [args[2] for _, args in sim.reports()]
    assert pendings and all(p and p > 0 for p in pendings), \
        "проверка и правда видела непустую очередь"
    assert all(args[1] is None for _, args in sim.reports())


async def test_queue_drains_and_the_alarm_is_cleared(sim: Sim) -> None:
    sim.telegram.enqueue(4)
    sim.telegram.withhold = True
    async with running(sim) as go:
        await go(700)
        assert sim.registry.hearing() is False
        sim.telegram.withhold = False
        await go(900)

    assert sim.registry.hearing() is True
    assert sim.processed == [500, 501, 502, 503], "очередь разобрана, а не выброшена"
    _, reason, pending = sim.last_report()
    assert reason is None
    assert pending == 0


async def test_unknown_queue_is_not_an_alarm(sim: Sim) -> None:
    """Telegram не ответил на проверку — длина очереди неизвестна, а не «сбой»."""
    sim.telegram.info_fail = tg.TelegramError(0, "транспорт: ConnectError: refused")
    async with running(sim) as go:
        await go(900)

    assert all(ok for _, ok in sim.pulse)
    assert sim.reports(), "отчёт о приёме уходит и без длины очереди"
    assert all(args[1] is None and args[2] is None for _, args in sim.reports())


def test_queue_window_rules() -> None:
    """Окно «стоит»: только непустая очередь при том же offset; пустая — сброс."""
    rec = Reception(0.0)
    rec.observe_queue(3, offset=10, now=0.0)
    rec.observe_queue(3, offset=10, now=200.0)
    assert rec.stuck_for(200.0) == 200.0
    rec.observe_queue(None, offset=10, now=260.0)
    assert rec.stuck_for(260.0) == 260.0, "неизвестная длина окно не сбрасывает"
    assert rec.pending is None
    rec.observe_queue(5, offset=11, now=300.0)
    assert rec.stuck_for(300.0) == 0.0, "offset сдвинулся — окно заново"
    rec.observe_queue(0, offset=11, now=400.0)
    assert rec.stuck_for(900.0) == 0.0


# ------------------------------------------------- правило 3: вторая копия
async def test_second_poller_with_the_same_token_is_flagged(sim: Sim) -> None:
    """Чужой цикл перебивает наш: часть заходов — 409, часть — ответы.

    Глухоты нет (ответы идут), очередь пуста (её разбирает чужой) — видно это
    только по самому 409. Когда чужой замолкает, окно закрывается само.
    """
    real = sim.telegram._get_updates
    rival = {"on": True, "n": 0}

    def contested(params: dict[str, Any]) -> list[dict[str, Any]]:
        rival["n"] += 1
        if rival["on"] and rival["n"] % 2:
            sim.clock.now += 3.0
            raise tg.TelegramError(409, OTHER_409)
        return real(params)

    sim.telegram._get_updates = contested  # type: ignore[method-assign]
    async with running(sim) as go:
        await go(300)
        assert sim.poller.deaf_for() < 60
        assert sim.registry.hearing() is False
        assert sim.last_report()[1] == reception.CONFLICT_TEXT
        rival["on"] = False
        await go(300 + reception.CONFLICT_WINDOW + 120)

    assert sim.registry.hearing() is True
    assert sim.last_report()[1] is None


def test_single_conflict_is_not_a_second_poller() -> None:
    """Разовый 409 бывает от ручной пробы тем же токеном — не повод краснеть."""
    rec = Reception(0.0)
    rec.answered(10.0)
    rec.failed(tg.TelegramError(409, OTHER_409), 20.0)
    assert rec.problem(30.0) is None
    rec.failed(tg.TelegramError(409, WEBHOOK_409), 40.0)
    rec.failed(tg.TelegramError(409, WEBHOOK_409), 50.0)
    assert rec.problem(60.0) is None, "вебхук — не вторая копия, его лечит поллер"


def test_problems_are_ranked_from_the_fullest_outage() -> None:
    rec = Reception(0.0)
    for at in (1.0, 2.0, 3.0):
        rec.failed(tg.TelegramError(409, OTHER_409), at)
    rec.observe_queue(7, offset=1, now=0.0)
    problem = rec.problem(reception.DEAF_AFTER + 1)
    assert problem is not None and problem.kind == "deaf"
    rec.answered(reception.DEAF_AFTER + 1)
    problem = rec.problem(reception.DEAF_AFTER + 2)
    assert problem is not None and problem.kind == "stuck", "конфликты старше окна"


# --------------------------------------- правило 4: переживает перезапуск
class FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        return self.rows


class FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.conn = FakeConn(rows)

    @contextlib.asynccontextmanager
    async def _acquire(self) -> Any:
        yield self.conn

    def acquire(self) -> Any:
        return self._acquire()


@contextlib.contextmanager
def _no_scope() -> Iterator[None]:
    yield


def _row(token: str) -> dict[str, Any]:
    return {"id": 7, "tenant_id": 1, "bot_id": 100, "username": "devon_sd_bot",
            "token": token, "update_offset": 55}


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> tuple[PollerRegistry, FakePool,
                                                       list[str]]:
    started: list[str] = []

    async def run(self: BotPoller) -> None:
        started.append(self._token)
        await self._stop.wait()

    monkeypatch.setattr(BotPoller, "run", run)
    monkeypatch.setattr(poller_mod, "system_scope", _no_scope)
    monkeypatch.setattr(poller_mod.box, "decrypt", lambda stored, ad: f"plain:{stored}")
    fake = FakePool([_row("enc:2:1:first")])
    monkeypatch.setattr(poller_mod, "pool", lambda: fake)
    return PollerRegistry(), fake, started


async def _stop_all(reg: PollerRegistry) -> None:
    for task in list(reg._tasks.values()):
        task.cancel()
    await asyncio.gather(*reg._tasks.values(), return_exceptions=True)


async def test_restarted_loop_keeps_counting_deafness(
        registry: tuple[PollerRegistry, FakePool, list[str]]) -> None:
    """Сторож переподнял цикл — это тот же бот, отсрочки заново он не получает.

    Иначе цикл, зависающий раз в две минуты, красным не стал бы никогда: каждый
    перезапуск отсчитывал бы глухоту с нуля.
    """
    reg, _, started = registry
    await reg.sync()
    await asyncio.sleep(0)
    first = reg._pollers[7]
    first.reception.started_at -= reception.DEAF_AFTER + 60   # давно без ответа
    first._polled_at -= poller_mod.STALL_AFTER + 1            # и завис

    await reg.sync()
    await asyncio.sleep(0)
    second = reg._pollers[7]
    assert second is not first and len(started) == 2
    assert second.reception is first.reception
    assert second.idle_for() < 1.0, "сторож доволен: цикл новый"
    assert reg.hearing() is False, "а пульс нет: Telegram молчит всё так же"
    await _stop_all(reg)


async def test_new_token_starts_a_new_reception(
        registry: tuple[PollerRegistry, FakePool, list[str]]) -> None:
    reg, fake, started = registry
    await reg.sync()
    await asyncio.sleep(0)
    old = reg._pollers[7].reception
    old.started_at -= reception.DEAF_AFTER + 60

    fake.conn.rows = [_row("enc:2:1:second")]
    await reg.sync()
    await asyncio.sleep(0)
    assert started == ["plain:enc:2:1:first", "plain:enc:2:1:second"]
    assert reg._pollers[7].reception is not old, "приём нового токена ещё не проверяли"
    assert reg._pollers[7]._offset == 55, "новый цикл продолжает с сохранённого offset"
    assert reg.hearing() is True

    fake.conn.rows = []
    await reg.sync()
    assert reg._receptions == {}, "ушедший бот не оставляет состояния"
    await _stop_all(reg)


async def test_registry_restarts_the_loop_when_token_is_saved_again(
        registry: tuple[PollerRegistry, FakePool, list[str]]) -> None:
    """Сохранение токена на вкладке «Бот» обязано доехать до опроса.

    Раньше цикл продолжал спрашивать Telegram старым токеном из памяти, а
    переподнятый цикл к тому же снимает вебхук при старте.
    """
    reg, _, started = registry
    await reg.sync()
    await asyncio.sleep(0)
    await reg.sync()  # тот же шифротекст — цикл не трогаем
    await asyncio.sleep(0)
    assert started == ["plain:enc:2:1:first"]
    await _stop_all(reg)


# ---------------------------------------- правило 5: отчёт раз в минуту
async def test_report_goes_out_once_a_minute(sim: Sim) -> None:
    """Экран считает на минуты; чаще — лишний вызов и запись на каждый опрос."""
    async with running(sim) as go:
        await go(1800)

    times = [at for at, _ in sim.reports()]
    assert 15 <= len(times) <= 31
    assert all(b - a >= poller_mod.CHECK_EVERY for a, b in itertools.pairwise(times))
    # Одна проверка очереди на отчёт; последняя могла не успеть записаться до
    # остановки симуляции.
    assert sim.telegram.count("getWebhookInfo") - len(times) in (0, 1)


async def test_heard_at_is_written_only_after_a_real_answer(sim: Sim) -> None:
    """«Telegram ответил при старте» было бы неправдой — до ответа heard_at не трогаем."""
    sim.telegram.fail = tg.TelegramError(502, "Bad Gateway")
    async with running(sim) as go:
        await go(120)
        assert all(args[0] is None for _, args in sim.reports())
        sim.telegram.fail = None
        await go(400)

    heard_ago = sim.last_report()[0]
    assert isinstance(heard_ago, float) and 0 <= heard_ago < poller_mod.CHECK_EVERY + 30


async def test_report_failure_never_stops_polling(
        sim: Sim, monkeypatch: pytest.MonkeyPatch) -> None:
    """Диагностика не имеет права уронить опрос: сбой отчёта — строка в логе."""
    async def broken(self: BotPoller, problem: Problem | None, now: float) -> None:
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(BotPoller, "_write_reception", broken)
    sim.telegram.trickle = True
    async with running(sim) as go:
        await go(600)
    assert len(sim.processed) > 10


def test_screen_waits_longer_than_the_slowest_report() -> None:
    """Экран назовёт службу неработающей, только если отчёта нет дольше худшего круга.

    Худший круг — отчёт стал нужен в начале захода, который висит до своего
    предела, а потом ещё проверка очереди до своего; плюс перезапуск зависшего
    цикла сторожем.
    """
    info_limit = tg.deadline("getWebhookInfo", None) + poller_mod.HARD_MARGIN
    slowest = poller_mod.CHECK_EVERY + _long_limit() + info_limit + poller_mod.ERROR_SLEEP
    assert slowest < reception.STATE_STALE_AFTER
    restart = poller_mod.STALL_AFTER + poller_mod.REFRESH_BOTS_EVERY + slowest
    assert restart < reception.STATE_STALE_AFTER


# ------------------------------------------------------ причина словами
def test_reasons_are_told_apart() -> None:
    """Разные поломки — разные ответы на вопрос «что чинить»."""
    texts = {
        reception.describe_failure(TimeoutError()),
        reception.describe_failure(tg.TelegramError(409, WEBHOOK_409)),
        reception.describe_failure(tg.TelegramError(409, OTHER_409)),
        reception.describe_failure(tg.TelegramError(0, "транспорт: ConnectError")),
        reception.describe_failure(tg.TelegramError(502, "Bad Gateway")),
    }
    assert len(texts) == 5
    assert "вебхук" in reception.describe_failure(tg.TelegramError(409, WEBHOOK_409))
    assert "ещё одна программа" in reception.describe_failure(
        tg.TelegramError(409, OTHER_409))


def test_client_timeout_reads_like_our_own_limit() -> None:
    """ReadTimeout клиента и наш предел — один и тот же сбой для человека."""
    ours = reception.describe_failure(TimeoutError())
    client = reception.describe_failure(tg.TelegramError(0, "транспорт: ReadTimeout"))
    assert ours == client


def test_reason_never_carries_proxy_credentials_or_a_token() -> None:
    """Текст уходит на экран администратора теннанта (И-7)."""
    text = reception.describe_failure(tg.TelegramError(
        0, "транспорт: ProxyError: socks5h://devon:s3cr3t@10.0.0.7:1080 refused"))
    assert "s3cr3t" not in text
    assert "devon:" not in text
    assert "10.0.0.7:1080" in text, "адрес прокси без пароля помогает понять, какой"

    secret = "123456789:" + "A" * 35
    text = reception.describe_failure(tg.TelegramError(
        500, f"Internal: /bot{secret}/getUpdates"))
    assert "A" * 35 not in text


@pytest.mark.parametrize(("seconds", "text"), [
    (20.0, "меньше минуты назад"), (600.0, "10 мин назад"),
    (7200.0, "2 ч назад"), (3 * 86400.0, "3 дн. назад"),
])
def test_ago(seconds: float, text: str) -> None:
    assert reception.ago(seconds) == text


# ---------------------------------------------------- правило 6: вебхук
async def test_webhook_409_is_healed_at_once(sim: Sim) -> None:
    """Свой вебхук снимается сразу, и следующий заход идёт без паузы на ошибку."""
    base = get_settings().public_base_url.rstrip("/")
    async with running(sim) as go:
        await go(30)
        sim.telegram.webhook_url = f"{base}/tg/{uuid.uuid4()}"
        started = sim.t
        await go(started + 60)

    assert sim.telegram.webhook_url == ""
    assert sim.telegram.count("deleteWebhook") == 2, "при старте и ещё раз — на 409"
    healed = [t for t, m in sim.telegram.calls if m == "deleteWebhook"][-1]
    assert healed - started < poller_mod.POLL_TIMEOUT + 5, "без ERROR_SLEEP и без часа"
    assert all(sql != poller_mod._SQL_SUSPEND for _, sql, _ in sim.writes)
    assert all(ok for _, ok in sim.pulse)


async def test_foreign_webhook_suspends_the_bot(sim: Sim) -> None:
    """Вебхук на чужой адрес — токен у третьих лиц: не снимать молча, а остановиться."""
    async with running(sim) as go:
        await go(10)
        sim.telegram.webhook_url = "https://collector.example/hook"
        await go(600)  # цикл обязан завершиться сам, не дожидаясь конца

    assert any(sql == poller_mod._SQL_SUSPEND for _, sql, _ in sim.writes), \
        "бот приостановлен"
    assert sim.telegram.webhook_url == "https://collector.example/hook", \
        "чужой вебхук не трогаем"
    assert sim.t < 600


def test_own_webhook_is_recognised_by_our_address() -> None:
    base = get_settings().public_base_url.rstrip("/")
    assert poller_mod.is_own_webhook(f"{base}/tg/{uuid.uuid4()}")
    assert not poller_mod.is_own_webhook("https://collector.example/tg/1")
    assert not poller_mod.is_own_webhook(f"{base}.collector.example/tg/1")


# -------------------------------------------------- живая база: схема
ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
live = pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL")


@live
async def test_reception_round_trips_through_the_schema(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Отчёт поллера, экран и вкладка «Бот» ходят в настоящие колонки миграции 0022.

    Расхождение схемы с кодом уже стоило этому проекту недели (`options` в 0007):
    подделка базы его не видит, видит только живая.
    """
    from b24bot.api import app_ui
    from b24bot.crypto import box

    uniq = uuid.uuid4().hex[:8]
    tenant = await db.fetchval(
        "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
        "VALUES ($1,'Теннант',$2,$3,'active') RETURNING id",
        f"t{uniq}", f"member-{uniq}", f"{uniq}.bitrix24.ru")
    bot_id = int(uuid.uuid4().int % 10**9)
    kid = get_settings().master_key_id
    token = f"{bot_id}:" + "A" * 35
    ref = await db.fetchval(
        "INSERT INTO tg_bots (tenant_id, bot_id, username, token, token_kid, "
        "webhook_secret, webhook_secret_kid, status, mode) "
        "VALUES ($1,$2,'bot',$3,$4,$5,$4,'active','polling') RETURNING id",
        tenant, bot_id,
        box.encrypt(token, box.aad("tg_bots", "token", tenant, bot_id)), kid,
        box.encrypt("hook", box.aad("tg_bots", "webhook_secret", tenant, bot_id)))

    async def screen() -> Any:
        return await db.fetchrow(app_ui._BOT_SELECT, tenant)

    row = await screen()
    assert row["poll_checked_ago"] is None and reception.on_screen(row) is None, \
        "до первого отчёта — не тревога"

    # Отчёт глухого поллера: ни одного ответа, очередь 19.
    now = time.monotonic()
    p = BotPoller(ref, tenant, bot_id, "bot", token, offset=0,
                  reception=Reception(now - 1000))
    p.reception.failed(TimeoutError(), now)
    p.reception.observe_queue(19, 0, now)
    problem = p.reception.problem(now)
    assert problem is not None
    await p._write_reception(problem, now)
    row = await screen()
    assert row["poll_error"] == problem.text
    assert row["queue_pending"] == 19
    assert row["heard_ago"] is None
    assert 0 <= row["poll_checked_ago"] < 60
    assert problem.text in (reception.on_screen(row) or "")

    # Telegram ответил полторы минуты назад — heard_at пересчитан в часы базы.
    p.reception.answered(now - 90)
    p.reception.observe_queue(0, 0, now)
    await p._write_reception(None, now)
    row = await screen()
    assert row["poll_error"] is None and row["queue_pending"] == 0
    assert 80 < row["heard_ago"] < 120
    assert reception.on_screen(row) is None

    # Служба бота замолчала: отчёта нет двадцать минут.
    await db.execute("UPDATE tg_bots SET poll_checked_at = now() - interval '20 min' "
                     "WHERE id = $1", ref)
    assert "Служба бота" in (reception.on_screen(await screen()) or "")

    # Подключение токена заново — наблюдение начинается с нуля.
    async def get_me(*args: object, **kwargs: object) -> dict[str, Any]:
        return {"id": bot_id, "username": "bot", "can_read_all_group_messages": True}

    async def ok(*args: object, **kwargs: object) -> bool:
        return True

    async def webhook_info(*args: object, **kwargs: object) -> dict[str, Any]:
        return {"url": "", "pending_update_count": 0}

    for name, fake in (("get_me", get_me), ("delete_webhook", ok),
                       ("set_chat_menu_button", ok), ("get_webhook_info", webhook_info)):
        monkeypatch.setattr(tg, name, fake)
    await db.execute("UPDATE tg_bots SET poll_error = 'старая причина', "
                     "queue_pending = 3, heard_at = now() - interval '1 day' "
                     "WHERE id = $1", ref)
    _, kind = await app_ui._connect_bot({"id": tenant, "b24_domain": "x"}, token)
    assert kind == "ok"
    row = await screen()
    assert row["poll_error"] is None and row["queue_pending"] is None
    assert row["heard_ago"] is None
    assert 0 <= row["poll_checked_ago"] < 60
    assert reception.on_screen(row) is None

    # «Проверить» у работающего бота отметку не освежает — иначе прятала бы
    # неработающую службу; возврат из error — освежает.
    await db.execute("UPDATE tg_bots SET poll_checked_at = now() - interval '20 min' "
                     "WHERE id = $1", ref)
    await app_ui._recheck_bot({"id": tenant, "b24_domain": "x"})
    assert (await screen())["poll_checked_ago"] > 600
    await db.execute("UPDATE tg_bots SET status = 'error', poll_error = 'старая' "
                     "WHERE id = $1", ref)
    await app_ui._recheck_bot({"id": tenant, "b24_domain": "x"})
    row = await screen()
    assert row["status"] == "active" and row["poll_error"] is None
    assert 0 <= row["poll_checked_ago"] < 60

    assert await p._write_state(poller_mod._SQL_SUSPEND, "чужой вебхук")
    assert await db.fetchval("SELECT status FROM tg_bots WHERE id = $1", ref) == "suspended"
