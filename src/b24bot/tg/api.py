"""Минимальный клиент Telegram Bot API.

Инвариант И-4: хост только `api.telegram.org` (или явно настроенный прокси теннанта),
никогда из входящих данных. Прямой путь по IP этого не меняет: в TLS называется
`api.telegram.org`, и сертификат проверяется по этому имени (`Route`).

Полноценный слой бота на aiogram придёт отдельно; здесь ровно то, что нужно для
подключения бота теннанта: проверить токен, узнать бота, поставить и снять вебхук.
"""
from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from b24bot.core.config import TELEGRAM_HOST, get_settings

log = logging.getLogger(__name__)

TIMEOUT = 20.0
# Long polling держит соединение дольше обычного запроса. HTTP-таймаут ОБЯЗАН его
# превышать, иначе клиент рвёт связь раньше ответа Telegram и цикл вырождается в
# бесконечную серию таймаутов.
POLL_HTTP_MARGIN = 15.0
# Скачивание файла до 20 МБ (tg/files.py).
FILE_TIMEOUT = 120.0
ALLOWED_UPDATES = ["message", "edited_message", "callback_query",
                   "my_chat_member", "chat_member"]

# Сколько ждать одного соединения. Короче общего таймаута: закрытый путь должен
# стоить секунд, а не всего вызова.
CONNECT_TIMEOUT = 3.0
# Сколько соединений пробовать на одном пути, прежде чем идти следующим. Прямой
# путь на боевом сервере теряет соединения вразброс: 23.09.2026 из 40 попыток раз
# в 3 с не установилось 5, поодиночке, и следующая попытка проходила за 0.04 с.
# Ожидание дольше не помогает (8 с — тот же отказ): соединение либо сразу есть,
# либо его не будет, а новое проходит. Первая версия уходила на прокси после
# ОДНОГО отказа — и на пять минут возвращала бота туда, где крупный апдейт не
# проходит, то есть ровно в аварию, которую чинила.
CONNECT_ATTEMPTS = 3
# Сколько не пробовать путь первым после того, как все попытки не дали соединения.
ROUTE_COOLDOWN = 60.0

# Ошибки, при которых запрос гарантированно НЕ ушёл в Telegram: соединение не
# установлено (TCP, TLS, SOCKS). Только после них вызов можно повторить другим
# путём. После таймаута чтения — нельзя: `sendMessage` мог дойти, и повтор
# отправил бы сообщение второй раз.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)


@dataclass(frozen=True)
class Route:
    """Один путь до api.telegram.org.

    Зачем их несколько — живая авария 23.09.2026 (docs/10-architecture.md §2).
    Прокси, через который ходил бот, замораживает любое соединение после ~16 КБ
    входящих данных: TLS-рукопожатие съедает 5.5 КБ, и ответ крупнее ~11 КБ не
    доходит никогда. Апдейт с реплаем на карточку задачи весит 14.7 КБ — он встал
    первым в очереди, и бот пять часов не видел ни одного нажатия. Прямой путь по
    IP при этом отдаёт те же ответы за 0.2 с, а DNS-адрес `api.telegram.org`
    с сервера закрыт. Поэтому: прямые адреса первыми, прокси — запасным.
    """
    name: str              # для лога и учёта отказов; без логина и пароля прокси
    origin: str            # https://149.154.167.220 или https://api.telegram.org
    proxy: str | None = None
    sni: str | None = None  # имя для TLS, когда в адресе стоит IP


# Путь → момент, до которого он не пробуется первым. Общее на процесс: отказ,
# увиденный одним вызовом, экономит время всем следующим.
_down_until: dict[str, float] = {}


def routes(proxy_base: str | None = None) -> list[Route]:
    """Пути в порядке предпочтения. Только из настроек, никогда из входящих данных."""
    s = get_settings()
    if proxy_base:
        return [Route("base", proxy_base.rstrip("/"), s.tg_proxy)]
    out = [Route(ip, f"https://{_url_host(ip)}", sni=TELEGRAM_HOST)
           for ip in s.tg_direct_ips]
    # Настроенный прокси означает, что адрес из DNS с этого хоста закрыт: пробовать
    # его — только тратить время. Без прокси DNS-адрес и есть обычный путь.
    if s.tg_proxy:
        out.append(Route("прокси", f"https://{TELEGRAM_HOST}", proxy=s.tg_proxy))
    else:
        out.append(Route(TELEGRAM_HOST, f"https://{TELEGRAM_HOST}"))
    return out


def _url_host(ip: str) -> str:
    return f"[{ip}]" if ipaddress.ip_address(ip).version == 6 else ip


def _ordered(candidates: list[Route]) -> list[Route]:
    """Сначала пути без недавнего отказа, в порядке настройки; остальные — после.

    Отказавшие не выбрасываются: если закрыто всё, пробовать всё равно надо.
    """
    now = time.monotonic()
    fresh = [r for r in candidates if _down_until.get(r.name, 0.0) <= now]
    return fresh + [r for r in candidates if r not in fresh]


def deadline(method: str, params: dict[str, Any] | None,
             proxy_base: str | None = None) -> float:
    """Худшее время одного `call`: на каждом пути все попытки ждали соединения,
    на последнем ещё и ответа. Свой предел поверх клиента обязан быть ДЛИННЕЕ
    (bot/poller.py): иначе он рубит вызов до того, как клиент узнал, что путь
    закрыт, и следующий заход снова начнётся с закрытого.
    """
    return (CONNECT_TIMEOUT * CONNECT_ATTEMPTS * len(routes(proxy_base))
            + http_timeout_for(method, params))


async def _request(verb: str, path: str, *, http_timeout: float,
                   json: dict[str, Any] | None = None,
                   proxy_base: str | None = None) -> httpx.Response:
    """Запрос к Telegram первым живым путём.

    Повтор — новым соединением, на том же пути до `CONNECT_ATTEMPTS` раз, затем
    на следующем, и только если соединение не установилось (`_NOT_SENT`). Любая
    другая ошибка уходит вызывающему как есть.
    """
    last: httpx.HTTPError | None = None
    for route in _ordered(routes(proxy_base)):
        extra: dict[str, Any] = {}
        if route.sni:
            extra = {"headers": {"Host": route.sni},
                     "extensions": {"sni_hostname": route.sni}}
        for _ in range(CONNECT_ATTEMPTS):
            try:
                async with _client(http_timeout, route) as http:
                    resp = await http.request(verb, route.origin + path, json=json,
                                              **extra)
            except _NOT_SENT as exc:
                last = exc
                continue
            if _down_until.pop(route.name, None) is not None:
                log.info("Telegram: путь %s снова открыт", route.name)
            return resp
        if last is not None and _down_until.get(route.name, 0.0) <= time.monotonic():
            log.warning("Telegram: путь %s закрыт (%s, попыток %d) — иду следующим",
                        route.name, type(last).__name__, CONNECT_ATTEMPTS)
        _down_until[route.name] = time.monotonic() + ROUTE_COOLDOWN
    assert last is not None, "routes() всегда возвращает хотя бы один путь"
    raise last


class TelegramError(Exception):
    def __init__(self, code: int, description: str, *,
                 migrate_to_chat_id: int | None = None) -> None:
        self.code, self.description = code, description
        # Группа стала супергруппой: вызов со старым chat_id — отправку, getChat —
        # Telegram отвергает и сам называет новый (`ResponseParameters`). Весть о
        # переезде помимо двух служебных сообщений (domain/chat_migration.py).
        self.migrate_to_chat_id = migrate_to_chat_id
        super().__init__(f"Telegram {code}: {description}")


class TelegramInvalidToken(TelegramError):
    """401 — токен не тот или отозван в BotFather."""


def _client(timeout: float, route: Route) -> httpx.AsyncClient:
    """Единственное место, где создаётся HTTP-клиент к Telegram.

    Прокси берётся из настроек, а не из входящих данных (И-4).
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT)),
        proxy=route.proxy)


def http_timeout_for(method: str, params: dict[str, Any] | None) -> float:
    """Для long polling ждём дольше, чем Telegram держит соединение."""
    if method == "getUpdates" and params and params.get("timeout"):
        return float(params["timeout"]) + POLL_HTTP_MARGIN
    return TIMEOUT


def _transport_error(exc: Exception) -> TelegramError:
    # У таймаутов httpx пустой str(), поэтому имя класса обязательно:
    # без него в логе остаётся бесполезное «транспорт: ».
    detail = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    return TelegramError(0, f"транспорт: {detail[:150]}")


async def call(token: str, method: str, params: dict[str, Any] | None = None, *,
               proxy_base: str | None = None) -> Any:
    try:
        resp = await _request("POST", f"/bot{token}/{method}",
                              http_timeout=http_timeout_for(method, params),
                              json=params or {}, proxy_base=proxy_base)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _transport_error(exc) from exc

    if not data.get("ok"):
        code = int(data.get("error_code") or resp.status_code)
        desc = str(data.get("description") or "")
        if code == 401:
            raise TelegramInvalidToken(code, desc)
        raise TelegramError(code, desc, migrate_to_chat_id=_migrated_to(data))
    return data.get("result")


async def download_file(token: str, file_path: str) -> bytes:
    """Файл по `file_path` из `getFile` — теми же путями, что и вызовы API.

    Через прокси файл не скачивался вовсе: замерзание после ~16 КБ бьёт по любой
    фотографии, а не только по крупным апдейтам.
    """
    try:
        resp = await _request("GET", f"/file/bot{token}/{file_path}",
                              http_timeout=FILE_TIMEOUT)
    except httpx.HTTPError as exc:
        raise _transport_error(exc) from exc
    if resp.status_code != 200:
        # Без адреса: в нём токен.
        raise TelegramError(resp.status_code, "файл не скачан")
    return resp.content


def _migrated_to(data: dict[str, Any]) -> int | None:
    """Новый chat_id из ответа «group chat was upgraded to a supergroup chat»."""
    params = data.get("parameters")
    value = params.get("migrate_to_chat_id") if isinstance(params, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        return None
    return value


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


async def set_my_commands(token: str, commands: list[dict[str, str]], *,
                          scope: str | None = None,
                          proxy_base: str | None = None) -> bool:
    """Меню слеш-команд. Пустой список стирает меню для этой области.

    Область (`scope`) обязательна для всего, кроме умолчания: без неё Telegram
    пишет один список во все чаты сразу, и в личке появляются команды группы.
    """
    params: dict[str, Any] = {"commands": commands}
    if scope:
        params["scope"] = {"type": scope}
    result = await call(token, "setMyCommands", params, proxy_base=proxy_base)
    return bool(result)


async def get_my_commands(token: str, *, scope: str | None = None,
                          proxy_base: str | None = None) -> list[dict[str, str]]:
    params: dict[str, Any] = {}
    if scope:
        params["scope"] = {"type": scope}
    result = await call(token, "getMyCommands", params, proxy_base=proxy_base)
    return list(result or [])


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
