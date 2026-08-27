"""OAuth-код в обмен на токены — направление «Telegram → портал».

До сих пор привязка шла только в одну сторону: человек открывал приложение внутри
Битрикса, портал сам присылал его `AUTH_ID`, и оставалось связать это с телеграм-
аккаунтом одноразовым deep-link'ом. Путь рабочий, но начинается он там, где человека
в этот момент нет: он в чате поддержки, а не в портале.

Здесь собрано всё, что нужно для обратного направления: адрес экрана согласия
портала и обмен `code` на пару токенов. Правила те же, что и у обновления токена
(`b24/tokens.py`):

* хосты обмена — только из allowlist `OAUTH_HOSTS` (И-4), никогда из входящих данных;
* домен портала проверяется `is_trusted_portal_domain` и берётся из `tenants`,
  а не из query-строки колбэка;
* ответ с `error` не превращается в «пустой успех»: он возвращается как есть,
  и решение принимает вызывающий.

Что на живом портале **проверено** (27.08.2026, запросом без авторизации):
`https://<портал>/oauth/authorize/?client_id=…&response_type=code&state=…` отвечает
302 на экран входа Битрикс24.Net и **переносит наши параметры через логин**
(они уезжают в `oauth_proxy_params` base64) — то есть `state` возвращается к нам
в том же виде, в каком отправлен.

Что **не проверено** и требует одного живого входа человеком: принимает ли локальное
приложение свой `redirect_uri`. Документация Битрикса разрешает не передавать его
вовсе, если у приложения один обработчик, — тогда `code` приезжает на
зарегистрированный путь обработчика. Поэтому по умолчанию мы его не передаём, а
`code` принимаем на всех трёх путях, куда портал в состоянии его привезти
(docs/50-web-and-b24-app.md §2.10).
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from b24bot.core.config import OAUTH_HOSTS, get_settings, is_trusted_portal_domain

log = logging.getLogger(__name__)

AUTHORIZE_PATH = "/oauth/authorize/"
EXCHANGE_PATH = "/oauth/token/"  # обмен кода и refresh на пару токенов
CALLBACK_PATH = "/b24/oauth/callback"
HTTP_TIMEOUT = 30.0


def authorize_url(portal_domain: str, state: str, *,
                  redirect_uri: str | None = None) -> str:
    """Адрес экрана согласия портала.

    Домен приходит из `tenants.b24_domain`, но проверяется всё равно: подстановка
    чужого домена увела бы человека на чужой экран входа с нашим `client_id`.
    """
    if not is_trusted_portal_domain(portal_domain):
        raise ValueError(f"домен портала не прошёл проверку: {portal_domain!r}")
    params = {
        "client_id": get_settings().b24_client_id,
        "response_type": "code",
        "state": state,
    }
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    return f"https://{portal_domain}{AUTHORIZE_PATH}?{urlencode(params)}"


def redirect_uri() -> str | None:
    """Наш адрес приёма кода — или `None`, если его решено не передавать.

    Значение собирается из `public_base_url`, а не из запроса: адрес, по которому
    портал вернёт `code`, — это ровно то место, куда нельзя пускать чужой ввод.
    """
    s = get_settings()
    if not s.b24_oauth_redirect:
        return None
    base = s.public_base_url.rstrip("/")
    return f"{base}{CALLBACK_PATH}" if base.startswith("https://") else None


async def post_token(params: dict[str, str]) -> dict[str, Any] | None:
    """POST на oauth-хост из allowlist. Первый осмысленный ответ и есть результат.

    Осмысленный — это и успех, и отказ портала: на отказ фоллбэк на второй хост
    не поможет, а повтор сожжёт одноразовый `code`.
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        for host in OAUTH_HOSTS:
            try:
                resp = await http.post(f"https://{host}{EXCHANGE_PATH}", data=params)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("oauth-хост %s недоступен: %s", host, str(exc)[:120])
                continue
            if isinstance(data, dict) and (data.get("access_token") or data.get("error")):
                result: dict[str, Any] = data
                return result
    return None


async def exchange_code(code: str) -> dict[str, Any] | None:
    """`code` → пара токенов. `code` одноразовый: второй попытки у него нет."""
    s = get_settings()
    params = {
        "grant_type": "authorization_code",
        "client_id": s.b24_client_id,
        "client_secret": s.b24_client_secret,
        "code": code,
    }
    uri = redirect_uri()
    if uri:
        params["redirect_uri"] = uri
    return await post_token(params)


async def is_portal_admin(domain: str, access_token: str) -> bool:
    """Права администратора портала спрашиваем у самого Битрикса.

    `user.admin` отвечает про ТЕКУЩЕГО пользователя токена, и запрос идёт с нашего
    сервера на портал — подделать ответ по дороге нельзя. Отказ метода трактуется
    как «не администратор»: права выдаются только по явному «да».

    Живёт здесь, а не в обработчике placement, потому что дверей стало две: и
    открытие приложения в портале, и вход из Telegram обязаны решать это одинаково.
    """
    try:
        res = await rest_call(domain, "user.admin", access_token)
    except Exception as exc:  # любая беда здесь означает «не администратор»
        log.warning("user.admin недоступен: %s", str(exc)[:120])
        return False
    return bool(res.get("result"))


async def rest_call(domain: str, method: str, access_token: str,
                    params: dict[str, Any] | None = None) -> dict[str, Any]:
    """REST-вызов свежим токеном: `client_endpoint` собираем САМИ из домена (И-4).

    Отдельно от `b24/client.py` намеренно: там лимитер, ретраи и хранилище токенов,
    а здесь токен ещё не сохранён и теннант, строго говоря, ещё не опознан.
    """
    if not is_trusted_portal_domain(domain):
        raise ValueError(f"домен портала не прошёл проверку: {domain!r}")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(f"https://{domain}/rest/{method}",
                                 data={**(params or {}), "auth": access_token})
        payload: dict[str, Any] = resp.json()
        return payload
