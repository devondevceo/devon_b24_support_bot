"""Минимальный клиент Telegram Bot API.

Инвариант И-4: хост только `api.telegram.org` (или явно настроенный прокси теннанта),
никогда из входящих данных.

Полноценный слой бота на aiogram придёт отдельно; здесь ровно то, что нужно для
подключения бота теннанта: проверить токен, узнать бота, поставить и снять вебхук.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from b24bot.core.config import TELEGRAM_HOST, get_settings

log = logging.getLogger(__name__)

TIMEOUT = 20.0
# Long polling держит соединение дольше обычного запроса. HTTP-таймаут ОБЯЗАН его
# превышать, иначе клиент рвёт связь раньше ответа Telegram и цикл вырождается в
# бесконечную серию таймаутов.
POLL_HTTP_MARGIN = 15.0
ALLOWED_UPDATES = ["message", "edited_message", "callback_query",
                   "my_chat_member", "chat_member"]


class TelegramError(Exception):
    def __init__(self, code: int, description: str) -> None:
        self.code, self.description = code, description
        super().__init__(f"Telegram {code}: {description}")


class TelegramInvalidToken(TelegramError):
    """401 — токен не тот или отозван в BotFather."""


def _base(token: str, proxy_base: str | None = None) -> str:
    host = proxy_base.rstrip("/") if proxy_base else f"https://{TELEGRAM_HOST}"
    return f"{host}/bot{token}/"


def _client(timeout: float) -> httpx.AsyncClient:
    """Единственное место, где создаётся HTTP-клиент к Telegram.

    Прокси берётся из настроек, а не из входящих данных (И-4).
    """
    return httpx.AsyncClient(timeout=timeout, proxy=get_settings().tg_proxy)


def http_timeout_for(method: str, params: dict[str, Any] | None) -> float:
    """Для long polling ждём дольше, чем Telegram держит соединение."""
    if method == "getUpdates" and params and params.get("timeout"):
        return float(params["timeout"]) + POLL_HTTP_MARGIN
    return TIMEOUT


async def call(token: str, method: str, params: dict[str, Any] | None = None, *,
               proxy_base: str | None = None) -> Any:
    async with _client(http_timeout_for(method, params)) as http:
        try:
            resp = await http.post(_base(token, proxy_base) + method, json=params or {})
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # У таймаутов httpx пустой str(), поэтому имя класса обязательно:
            # без него в логе остаётся бесполезное «транспорт: ».
            detail = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
            raise TelegramError(0, f"транспорт: {detail[:150]}") from exc

    if not data.get("ok"):
        code = int(data.get("error_code") or resp.status_code)
        desc = str(data.get("description") or "")
        if code == 401:
            raise TelegramInvalidToken(code, desc)
        raise TelegramError(code, desc)
    return data.get("result")


async def get_me(token: str, *, proxy_base: str | None = None) -> dict[str, Any]:
    """Единственный способ узнать, чей это токен. Обязателен перед сохранением."""
    me: dict[str, Any] = await call(token, "getMe", proxy_base=proxy_base)
    return me


async def set_webhook(token: str, url: str, secret: str, *,
                      proxy_base: str | None = None) -> bool:
    ok: bool = await call(token, "setWebhook", {
        "url": url,
        "secret_token": secret,
        "allowed_updates": ALLOWED_UPDATES,
        "drop_pending_updates": True,
        "max_connections": 20,
    }, proxy_base=proxy_base)
    return ok


async def delete_webhook(token: str, *, proxy_base: str | None = None) -> bool:
    ok: bool = await call(token, "deleteWebhook", {"drop_pending_updates": False},
                          proxy_base=proxy_base)
    return ok


async def get_webhook_info(token: str, *, proxy_base: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = await call(token, "getWebhookInfo", proxy_base=proxy_base)
    return info


async def set_chat_menu_button(token: str, url: str, *, text: str = "Задачи",
                               chat_id: int | None = None,
                               proxy_base: str | None = None) -> bool:
    """Кнопка меню бота открывает мини-апп в личных чатах.

    Единственная часть регистрации мини-аппа, доступная через Bot API: короткое имя
    приложения (`/newapp`) заводится только в BotFather вручную. Поэтому кнопку
    ставим сами — человеку не нужно ничего настраивать, чтобы мини-апп открылся.

    **Без `chat_id` Telegram принимает вызов, но кнопку не показывает**, если у бота
    настроено меню команд: `getChatMenuButton` продолжает отвечать `commands`.
    Проверено на живом боте 16.08.2026. Поэтому кнопка ставится ещё и адресно —
    на `/start` и при завершении привязки, когда личный чат уже известен.
    """
    params: dict[str, Any] = {
        "menu_button": {"type": "web_app", "text": text[:16], "web_app": {"url": url}},
    }
    if chat_id is not None:
        params["chat_id"] = chat_id
    ok: bool = await call(token, "setChatMenuButton", params, proxy_base=proxy_base)
    return ok


async def send_message(token: str, chat_id: int, text: str, *,
                       thread_id: int | None = None,
                       reply_markup: dict[str, Any] | None = None,
                       proxy_base: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                              "disable_web_page_preview": True}
    if thread_id:
        params["message_thread_id"] = thread_id
    if reply_markup:
        params["reply_markup"] = reply_markup
    msg: dict[str, Any] = await call(token, "sendMessage", params, proxy_base=proxy_base)
    return msg


def token_looks_valid(token: str) -> bool:
    """Грубая проверка формы до сетевого вызова: <digits>:<35+ символов>."""
    if ":" not in token:
        return False
    left, _, right = token.partition(":")
    return left.isdigit() and len(left) >= 6 and len(right) >= 30


def bot_id_from_token(token: str) -> int | None:
    left, _, _ = token.partition(":")
    return int(left) if left.isdigit() else None
