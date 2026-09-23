"""Приём апдейтов: здоровым считается бот, которому Telegram ОТВЕЧАЕТ.

Живая авария 23.09.2026. Бот не отвечал ни на одну команду, а зелёным было всё:
контейнер `healthy`, `/health` — `ok`, экран приложения в Битриксе — «Интеграция
работает». Сторож 08.09 (tests/test_poller_watchdog.py) ловил ЗАВИСШИЙ цикл, а
этот цикл не висел: он честно оборачивался, и оборотом засчитывался и 409 от
Telegram, и отказ прокси, и таймаут. То есть цикл, в котором не прошёл ни один
`getUpdates`, числился живым сколько угодно.

Отсюда правила, которые здесь проверяются:

1. **Пульс — только пока Telegram отвечает.** Оборот без ответа — не здоровье.
2. **Причина глухоты видна человеку** (`tg_bots.poll_error`, миграция 0021) — один
   раз, когда глухота стала фактом, а не на каждом обороте; ответ Telegram её снимает.
3. **409 из-за вебхука лечится на месте.** Вкладка «Бот» ставила вебхук при каждом
   сохранении токена, и работающий цикл глох до перезапуска контейнера. Свой вебхук
   снимается сразу, чужой — это токен у третьих лиц, и бот приостанавливается.
4. **Новый токен доезжает до опроса.** Цикл держит токен в памяти; реестр сверяет
   шифротекст и переподнимает цикл.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from b24bot.bot import poller as poller_mod
from b24bot.bot.poller import BotPoller, PollerRegistry
from b24bot.core.config import get_settings
from b24bot.tg import api as tg

WEBHOOK_409 = ("Conflict: can't use getUpdates method while webhook is active; "
               "use deleteWebhook to delete the webhook first")
OTHER_409 = ("Conflict: terminated by other getUpdates request; make sure that only "
             "one bot instance is running")


def _poller(*, idle: float = 0.0, deaf: float = 0.0) -> BotPoller:
    p = BotPoller(1, 1, 100, "devon_sd_bot", "123:secret", offset=10)
    p._polled_at = time.monotonic() - idle
    p._heard_at = time.monotonic() - deaf
    return p


class Writes:
    """Что поллер пишет в `tg_bots`: SQL и параметры, без базы."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple[object, ...]]] = []

    def of(self, sql: str) -> list[tuple[object, ...]]:
        return [args for s, args in self.rows if s == sql]


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> Writes:
    w = Writes()

    async def write_state(self: BotPoller, sql: str, *args: object) -> bool:
        w.rows.append((sql, args))
        return True

    monkeypatch.setattr(BotPoller, "_write_state", write_state)
    return w


async def _no_commands(*args: object, **kwargs: object) -> object:
    return True


async def _run_for(p: BotPoller, seconds: float) -> None:
    task = asyncio.create_task(p.run())
    await asyncio.sleep(seconds)
    p.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ------------------------------------------------------------------ пульс
def test_fresh_poller_is_heard() -> None:
    """У свежего цикла есть DEAF_AFTER на первый ответ: выкатка не начинается с красного."""
    assert PollerRegistry().hearing() is True
    reg = PollerRegistry()
    reg._pollers[1] = _poller()
    assert reg.hearing() is True


def test_laps_without_answers_do_not_keep_the_pulse() -> None:
    """Ровно авария 23.09: цикл крутится (не завис), но Telegram не отвечает."""
    reg = PollerRegistry()
    reg._pollers[1] = _poller(idle=0.0, deaf=poller_mod.DEAF_AFTER + 1)
    assert reg._pollers[1].idle_for() < poller_mod.STALL_AFTER, "цикл не завис"
    assert reg.hearing() is False


def test_answer_restores_the_pulse() -> None:
    p = _poller(deaf=poller_mod.DEAF_AFTER + 1)
    p._on_success()
    assert p.deaf_for() < 1.0


def test_deafness_outlasts_the_short_poll_fallback() -> None:
    """Сеть, где длинный опрос не живёт, а короткий живёт, — не глухота.

    До отступления на короткий опрос проходит FALLBACK_AFTER заходов по
    POLL_HARD_LIMIT плюс паузы; предел глухоты обязан быть длиннее, иначе
    healthcheck краснел бы на сети, где бот работает.
    """
    fallback = poller_mod.FALLBACK_AFTER * (poller_mod.POLL_HARD_LIMIT
                                            + poller_mod.ERROR_SLEEP)
    assert fallback < poller_mod.DEAF_AFTER


@pytest.mark.parametrize("failure", [
    tg.TelegramError(409, OTHER_409),
    tg.TelegramError(0, "транспорт: ConnectError: [Errno 111] Connection refused"),
    tg.TelegramError(502, "Bad Gateway"),
])
async def test_failing_getupdates_turns_the_container_red(
        monkeypatch: pytest.MonkeyPatch, writes: Writes,
        failure: tg.TelegramError) -> None:
    """Каждый заход кончается ошибкой — пульс гаснет, хотя цикл не завис."""
    monkeypatch.setattr(poller_mod, "DEAF_AFTER", 0.05)
    monkeypatch.setattr(poller_mod, "ERROR_SLEEP", 0.01)

    async def fails(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(tg, "call", fails)
    monkeypatch.setattr(tg, "delete_webhook", _no_commands)
    monkeypatch.setattr(tg, "set_my_commands", _no_commands)
    monkeypatch.setattr(tg, "get_webhook_info", _no_commands)

    p = _poller()
    reg = PollerRegistry()
    reg._pollers[1] = p
    await _run_for(p, 0.3)

    assert p.idle_for() < poller_mod.STALL_AFTER, "сторож 08.09 тут молчит — цикл жив"
    assert reg.hearing() is False
    reported = writes.of(poller_mod._SQL_DEAF)
    assert reported, "причина глухоты обязана дойти до экрана"


# ---------------------------------------------------------- причина глухоты
async def test_reason_is_written_once_and_cleared_by_an_answer(
        monkeypatch: pytest.MonkeyPatch, writes: Writes) -> None:
    """Одна запись о глухоте, а не строка в базу каждые пять секунд."""
    monkeypatch.setattr(poller_mod, "DEAF_AFTER", 0.0)
    p = _poller(deaf=1.0)
    failure = tg.TelegramError(0, "транспорт: ConnectError: refused")
    for _ in range(5):
        await p._on_failure(failure)
    assert len(writes.of(poller_mod._SQL_DEAF)) == 1

    p._on_success()
    await p._mark_heard()
    assert writes.of(poller_mod._SQL_HEARD), "ответ Telegram снимает причину сразу"
    assert p._reported is None


async def test_short_blips_do_not_reach_the_screen(writes: Writes) -> None:
    """Единичный сбой сети — не повод пугать экран приложения."""
    p = _poller()
    await p._on_failure(TimeoutError())
    assert writes.of(poller_mod._SQL_DEAF) == []


async def test_heard_mark_is_throttled(writes: Writes) -> None:
    """Отметку читает экран, где счёт на минуты: не пишем её на каждый короткий опрос."""
    p = _poller()
    for _ in range(10):
        p._on_success()
        await p._mark_heard()
    assert len(writes.of(poller_mod._SQL_HEARD)) == 1


def test_reasons_are_told_apart() -> None:
    """Четыре разные поломки — четыре разных ответа на вопрос «что чинить»."""
    texts = {
        poller_mod.describe_failure(TimeoutError()),
        poller_mod.describe_failure(tg.TelegramError(409, WEBHOOK_409)),
        poller_mod.describe_failure(tg.TelegramError(409, OTHER_409)),
        poller_mod.describe_failure(tg.TelegramError(0, "транспорт: ConnectError")),
    }
    assert len(texts) == 4
    assert "вебхук" in poller_mod.describe_failure(tg.TelegramError(409, WEBHOOK_409))
    assert "ещё один процесс" in poller_mod.describe_failure(
        tg.TelegramError(409, OTHER_409))


def test_reason_never_carries_proxy_credentials() -> None:
    """Текст уходит на экран администратора теннанта."""
    text = poller_mod.describe_failure(tg.TelegramError(
        0, "транспорт: ProxyError: socks5h://devon:s3cr3t@10.0.0.7:1080 refused"))
    assert "s3cr3t" not in text
    assert "devon:" not in text
    assert "10.0.0.7:1080" in text, "адрес прокси без пароля помогает понять, какой"


# ------------------------------------------------------------------- вебхук
async def test_webhook_409_is_healed_at_once(
        monkeypatch: pytest.MonkeyPatch, writes: Writes) -> None:
    """Свой вебхук снимается сразу, и следующий заход идёт без паузы на ошибку.

    ERROR_SLEEP здесь нарочно час: уйди цикл в обычную ветку ошибки, второго
    захода за время теста не случилось бы вовсе.
    """
    monkeypatch.setattr(poller_mod, "ERROR_SLEEP", 3600.0)
    base = get_settings().public_base_url.rstrip("/")
    calls: list[str] = []
    deleted: list[str] = []

    async def telegram(token: str, method: str, params: object = None,
                       **kwargs: object) -> object:
        calls.append(method)
        if calls.count("getUpdates") == 1:
            raise tg.TelegramError(409, WEBHOOK_409)
        return []

    async def webhook_info(token: str, **kwargs: object) -> dict[str, Any]:
        return {"url": f"{base}/tg/{uuid.uuid4()}", "pending_update_count": 4}

    async def delete(token: str, **kwargs: object) -> bool:
        deleted.append(token)
        return True

    monkeypatch.setattr(tg, "call", telegram)
    monkeypatch.setattr(tg, "get_webhook_info", webhook_info)
    monkeypatch.setattr(tg, "delete_webhook", delete)
    monkeypatch.setattr(tg, "set_my_commands", _no_commands)

    p = _poller()
    await _run_for(p, 0.2)

    assert len(deleted) == 2, "при старте и ещё раз — на 409"
    assert calls.count("getUpdates") >= 2, "после снятия вебхука — сразу новый заход"
    assert writes.of(poller_mod._SQL_SUSPEND) == []


async def test_foreign_webhook_suspends_the_bot(
        monkeypatch: pytest.MonkeyPatch, writes: Writes) -> None:
    """Вебхук на чужой адрес — токен у третьих лиц: не снимать молча, а остановиться."""
    deleted: list[str] = []

    async def telegram(*args: object, **kwargs: object) -> object:
        raise tg.TelegramError(409, WEBHOOK_409)

    async def webhook_info(token: str, **kwargs: object) -> dict[str, Any]:
        return {"url": "https://collector.example/hook", "pending_update_count": 9}

    async def delete(token: str, **kwargs: object) -> bool:
        deleted.append(token)
        return True

    monkeypatch.setattr(tg, "call", telegram)
    monkeypatch.setattr(tg, "get_webhook_info", webhook_info)
    monkeypatch.setattr(tg, "delete_webhook", delete)
    monkeypatch.setattr(tg, "set_my_commands", _no_commands)

    p = _poller()
    await asyncio.wait_for(p.run(), timeout=2)  # цикл обязан завершиться сам

    assert writes.of(poller_mod._SQL_SUSPEND), "бот приостановлен"
    assert len(deleted) == 1, "только снятие при старте — чужой вебхук не трогаем"


def test_own_webhook_is_recognised_by_our_address() -> None:
    base = get_settings().public_base_url.rstrip("/")
    assert poller_mod.is_own_webhook(f"{base}/tg/{uuid.uuid4()}")
    assert not poller_mod.is_own_webhook("https://collector.example/tg/1")
    assert not poller_mod.is_own_webhook(f"{base}.collector.example/tg/1")


# --------------------------------------------------------------- смена токена
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


async def test_registry_restarts_the_loop_when_token_is_saved_again(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Сохранение токена на вкладке «Бот» обязано доехать до опроса.

    Раньше цикл продолжал спрашивать Telegram старым токеном из памяти, а
    переподнятый цикл к тому же снимает вебхук при старте.
    """
    started: list[str] = []

    async def run(self: BotPoller) -> None:
        started.append(self._token)
        await self._stop.wait()

    monkeypatch.setattr(BotPoller, "run", run)
    monkeypatch.setattr(poller_mod, "system_scope", _no_scope)
    monkeypatch.setattr(poller_mod.box, "decrypt", lambda stored, ad: f"plain:{stored}")

    reg = PollerRegistry()
    fake = FakePool([_row("enc:2:1:first")])
    monkeypatch.setattr(poller_mod, "pool", lambda: fake)

    await reg.sync()
    await asyncio.sleep(0)
    await reg.sync()  # тот же шифротекст — цикл не трогаем
    await asyncio.sleep(0)
    assert started == ["plain:enc:2:1:first"]

    fake.conn.rows = [_row("enc:2:1:second")]
    await reg.sync()
    await asyncio.sleep(0)
    assert started == ["plain:enc:2:1:first", "plain:enc:2:1:second"]
    assert reg._pollers[7]._offset == 55, "новый цикл продолжает с сохранённого offset"

    for task in list(reg._tasks.values()):
        task.cancel()
    await asyncio.gather(*reg._tasks.values(), return_exceptions=True)


# -------------------------------------------------------- живая база: схема
ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
live = pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL")


@live
async def test_state_statements_match_the_schema(db: Any) -> None:
    """Запросы поллера и экрана ходят в настоящие колонки миграции 0021.

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
    ref = await db.fetchval(
        "INSERT INTO tg_bots (tenant_id, bot_id, username, token, token_kid, "
        "webhook_secret, webhook_secret_kid, status, mode) "
        "VALUES ($1,$2,'bot',$3,$4,$5,$4,'active','polling') RETURNING id",
        tenant, bot_id,
        box.encrypt("123:secret", box.aad("tg_bots", "token", tenant, bot_id)), kid,
        box.encrypt("hook", box.aad("tg_bots", "webhook_secret", tenant, bot_id)))

    p = BotPoller(ref, tenant, bot_id, "bot", "123:secret", offset=0)
    reason = poller_mod.describe_failure(tg.TelegramError(409, OTHER_409))
    assert await p._write_state(poller_mod._SQL_DEAF, reason)

    async def state() -> Any:
        return await db.fetchrow(
            "SELECT mode, status, poll_error, "
            "extract(epoch FROM now() - heard_at)::float8 AS heard_ago "
            "FROM tg_bots WHERE id = $1", ref)

    row = await state()
    assert row["poll_error"] == reason
    assert row["heard_ago"] is None
    assert app_ui.deafness(row) is not None, "экран видит причину"

    assert await p._write_state(poller_mod._SQL_HEARD)
    row = await state()
    assert row["poll_error"] is None
    assert 0 <= row["heard_ago"] < 60
    assert app_ui.deafness(row) is None

    assert await p._write_state(poller_mod._SQL_SUSPEND, "чужой вебхук")
    assert await db.fetchval("SELECT status FROM tg_bots WHERE id = $1", ref) == "suspended"
