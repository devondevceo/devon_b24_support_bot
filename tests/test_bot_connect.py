"""Вкладка «Бот» приложения Б24: подключение и проверка не глушат работающий опрос.

Обе кнопки этой вкладки умели выключить исправного бота, и обе — молча:

* **«Подключить бота» / «Заменить бота»** ставили вебхук, хотя бот работает long
  polling-ом (`tg_bots.mode='polling'` — умолчание с миграции 0003). Пока на боте
  висит вебхук, Telegram отвечает на `getUpdates` 409 и не отдаёт ни одного
  апдейта; поллер снимал вебхук только при старте, то есть уже запущенный цикл
  глох до перезапуска контейнера. Вебхук на этом хосте к тому же мёртв в обе
  стороны (docs/10-architecture.md, «Транспорт Telegram»).
* **«Проверить подключение»** при любом сбое Telegram — сеть, прокси, 502 — ставила
  `status='error'`, а реестр поллера опрашивает только `active`/`pending`. Проверка,
  нажатая в неудачную секунду, останавливала бота до следующей удачной проверки.

И ещё одна неправда, закрытая здесь же: «Подключение в порядке» рядом с поллером,
который Telegram не слышит. Проверка ходит из другого процесса, и её успех ничего
не говорит о приёме.
"""
from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from b24bot.api import app_ui
from b24bot.core.config import get_settings
from b24bot.crypto import box
from b24bot.tg import api as tg

TENANT = {"id": 5, "b24_domain": "devondev.bitrix24.ru"}
BOT_ID = 123456789
TOKEN = f"{BOT_ID}:" + "A" * 35


class Conn:
    """Ровно те запросы, что делают `_connect_bot` и `_recheck_bot`."""

    def __init__(self, *, mode: str = "polling", poll_error: str | None = None,
                 heard_ago: float | None = 30.0) -> None:
        self.mode, self.poll_error, self.heard_ago = mode, poll_error, heard_ago
        self.executed: list[str] = []

    async def fetchval(self, sql: str, *args: object) -> object:
        assert "FROM tg_bots WHERE bot_id" in sql
        return None  # бот никому не принадлежит

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any]:
        if "INSERT INTO tg_bots" in sql:
            return {"webhook_id": uuid.uuid4(), "mode": self.mode}
        if "poll_error" in sql:
            return {"mode": self.mode, "status": "active", "poll_error": self.poll_error,
                    "heard_ago": self.heard_ago}
        assert "SELECT bot_id, username, token" in sql
        kid_token = box.encrypt(TOKEN, box.aad("tg_bots", "token", TENANT["id"], BOT_ID))
        return {"bot_id": BOT_ID, "username": "devon_sd_bot", "token": kid_token,
                "webhook_id": uuid.uuid4(), "webhook_secret": "x", "mode": self.mode}

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append(sql)
        return "UPDATE 1"


class Pool:
    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    @contextlib.asynccontextmanager
    async def _acquire(self) -> AsyncIterator[Conn]:
        yield self.conn

    def acquire(self) -> Any:
        return self._acquire()


class Telegram:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.webhook_error: tg.TelegramError | None = None
        self.webhook_url = ""

    async def get_me(self, token: str, **kwargs: object) -> dict[str, Any]:
        self.calls.append("getMe")
        return {"id": BOT_ID, "username": "devon_sd_bot",
                "can_read_all_group_messages": True}

    async def set_webhook(self, *args: object, **kwargs: object) -> bool:
        self.calls.append("setWebhook")
        return True

    async def delete_webhook(self, *args: object, **kwargs: object) -> bool:
        self.calls.append("deleteWebhook")
        return True

    async def get_webhook_info(self, *args: object, **kwargs: object) -> dict[str, Any]:
        self.calls.append("getWebhookInfo")
        if self.webhook_error is not None:
            raise self.webhook_error
        return {"url": self.webhook_url, "pending_update_count": 7}


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> Telegram:
    fake = Telegram()
    for name in ("get_me", "set_webhook", "delete_webhook", "get_webhook_info"):
        monkeypatch.setattr(tg, name, getattr(fake, name))
    return fake


def _use(monkeypatch: pytest.MonkeyPatch, conn: Conn) -> Conn:
    monkeypatch.setattr(app_ui, "pool", lambda: Pool(conn))
    return conn


# ---------------------------------------------------------------- подключение
async def test_connecting_a_polling_bot_never_sets_a_webhook(
        monkeypatch: pytest.MonkeyPatch, telegram: Telegram) -> None:
    """Вебхук оглушил бы работающий поллер. Снимаем, а не ставим."""
    _use(monkeypatch, Conn(mode="polling"))
    message, kind = await app_ui._connect_bot(TENANT, TOKEN)  # type: ignore[arg-type]
    assert "setWebhook" not in telegram.calls
    assert "deleteWebhook" in telegram.calls
    assert kind == "ok"
    assert "вебхук" not in message, "обещать установленный вебхук — неправда"


async def test_webhook_mode_still_sets_its_webhook(
        monkeypatch: pytest.MonkeyPatch, telegram: Telegram) -> None:
    """Режим вебхука не сломан: он заработает при переезде на хост с прямым доступом."""
    _use(monkeypatch, Conn(mode="webhook"))
    message, _ = await app_ui._connect_bot(TENANT, TOKEN)  # type: ignore[arg-type]
    assert "setWebhook" in telegram.calls
    assert "вебхук установлен" in message


# ------------------------------------------------------------------- проверка
async def test_transient_telegram_failure_does_not_switch_the_bot_off(
        monkeypatch: pytest.MonkeyPatch, telegram: Telegram) -> None:
    """Сеть моргнула в секунду проверки — это повод сказать, а не выключить."""
    conn = _use(monkeypatch, Conn())
    telegram.webhook_error = tg.TelegramError(0, "транспорт: ConnectError: refused")
    message, kind = await app_ui._recheck_bot(TENANT)  # type: ignore[arg-type]
    assert kind == "err"
    assert "не выключен" in message
    assert not any("status='error'" in sql for sql in conn.executed)


async def test_revoked_token_does_switch_the_bot_off(
        monkeypatch: pytest.MonkeyPatch, telegram: Telegram) -> None:
    """401 — токен отозван: бот действительно выключен, и это надо записать."""
    conn = _use(monkeypatch, Conn())
    telegram.webhook_error = tg.TelegramInvalidToken(401, "Unauthorized")
    message, kind = await app_ui._recheck_bot(TENANT)  # type: ignore[arg-type]
    assert kind == "err"
    assert any("status='error'" in sql for sql in conn.executed)
    assert "новый токен" in message


async def test_recheck_does_not_call_a_deaf_bot_fine(
        monkeypatch: pytest.MonkeyPatch, telegram: Telegram) -> None:
    """Telegram ответил НА ПРОВЕРКУ, но поллер его не слышит — «в порядке» нельзя."""
    reason = "этого бота опрашивает ещё один процесс с тем же токеном (409)"
    _use(monkeypatch, Conn(poll_error=reason, heard_ago=1500.0))
    message, kind = await app_ui._recheck_bot(TENANT)  # type: ignore[arg-type]
    assert kind == "err"
    assert "не забирает сообщения" in message
    assert "в порядке" not in message


async def test_recheck_of_a_hearing_bot_is_fine(
        monkeypatch: pytest.MonkeyPatch, telegram: Telegram) -> None:
    _use(monkeypatch, Conn(heard_ago=20.0))
    message, kind = await app_ui._recheck_bot(TENANT)  # type: ignore[arg-type]
    assert kind == "ok"
    assert "в порядке" in message


async def test_recheck_removes_our_own_leftover_webhook(
        monkeypatch: pytest.MonkeyPatch, telegram: Telegram) -> None:
    """Вебхук, поставленный прежней версией этой же вкладки, снимается проверкой."""
    conn = _use(monkeypatch, Conn())
    base = get_settings().public_base_url
    # Тот же адрес, который вернёт подделка базы, — наш собственный вебхук.
    webhook_id = uuid.uuid4()

    async def fetchrow(sql: str, *args: object) -> dict[str, Any]:
        row = await Conn.fetchrow(conn, sql, *args)
        if "webhook_id" in row:
            row["webhook_id"] = webhook_id
        return row

    monkeypatch.setattr(conn, "fetchrow", fetchrow)
    telegram.webhook_url = f"{base}/tg/{webhook_id}"
    _, kind = await app_ui._recheck_bot(TENANT)  # type: ignore[arg-type]
    assert "deleteWebhook" in telegram.calls
    assert kind == "ok"
