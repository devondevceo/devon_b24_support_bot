"""Пути до Telegram: прямой адрес первым, прокси — запасным.

Живая авария 23.09.2026. Бот пять часов не видел ни одного нажатия кнопки при
зелёном всём. Прокси, через который он ходил к Telegram, замораживает любое
соединение после ~16 КБ входящих данных (замерено на всех трёх прокси
провайдера: обрыв на 13.7 КБ тела ответа при любом объёме). TLS-рукопожатие
с api.telegram.org само весит 5.5 КБ, и апдейт с реплаем на карточку задачи —
14.7 КБ — не доходил никогда. Он стоял первым в очереди, а за ним — всё
остальное. Прямой путь по IP при этом отдавал те же 15.9 КБ за 0.2 с.

Отсюда правила, которые здесь проверяются:

1. **Прямой адрес первым, и с настоящим именем в TLS.** Иначе либо не пройдёт
   проверка сертификата, либо её придётся выключить — и токен уйдёт любому,
   кто окажется по этому адресу.
2. **Следующий путь — только если запрос точно не ушёл.** После таймаута
   чтения `sendMessage` мог дойти, и повтор другим путём задвоил бы сообщение.
3. **Отказ пути запоминается:** закрытый путь стоит секунды один раз, а не на
   каждом вызове.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from b24bot.core.config import TELEGRAM_HOST, Settings
from b24bot.tg import api as tg

IP = "149.154.167.220"
PROXY = "socks5h://user:secret@10.0.0.1:1080"
TOKEN = "123456:" + "A" * 35
BASE = {"database_url": "postgresql://x@h/db",
        "master_key": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"}

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _fresh_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Память об отказах — общая на процесс; тесты не должны делить её."""
    monkeypatch.setattr(tg, "_down_until", {})


def _settings(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Settings:
    s = Settings(**BASE, **kw)
    monkeypatch.setattr(tg, "get_settings", lambda: s)
    return s


def _network(monkeypatch: pytest.MonkeyPatch,
             by_route: dict[str, Handler]) -> dict[str, list[httpx.Request]]:
    """Подменяет сеть: у каждого пути свой ответ. Возвращает журнал запросов."""
    seen: dict[str, list[httpx.Request]] = {name: [] for name in by_route}

    def client(timeout: float, route: tg.Route) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            seen[route.name].append(request)
            return by_route[route.name](request)
        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(tg, "_client", client)
    return seen


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"username": "devon_sd_bot"}})


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("timed out", request=request)


def _frozen(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("", request=request)


# ------------------------------------------------------------------ порядок
def test_direct_address_goes_first_and_proxy_is_the_fallback(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, tg_proxy_url=PROXY)
    names = [r.name for r in tg.routes()]
    assert names == [IP, "прокси"]


def test_proxy_route_name_carries_no_credentials(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Имя пути уходит в лог — логин и пароль прокси туда попасть не должны (И-7)."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    for route in tg.routes():
        assert "secret" not in route.name and "user" not in route.name


def test_without_proxy_the_dns_address_is_the_fallback(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Без прокси адрес из DNS и есть обычный путь — на другом хосте он открыт."""
    _settings(monkeypatch)
    routes = tg.routes()
    assert [r.name for r in routes] == [IP, TELEGRAM_HOST]
    assert routes[-1].origin == f"https://{TELEGRAM_HOST}" and routes[-1].sni is None


def test_proxy_configured_means_dns_address_is_not_tried(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Прокси настраивают, потому что DNS-адрес закрыт: пробовать его — терять время."""
    _settings(monkeypatch, tg_proxy_url=PROXY, tg_api_ips="")
    assert [r.name for r in tg.routes()] == ["прокси"]


# ------------------------------------------------------- прямой путь по IP
def test_direct_route_names_telegram_in_tls_and_in_host(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Адрес — IP, но имя в TLS и в Host — api.telegram.org.

    По этому имени проверяется сертификат: чужой сервер по этому адресу
    проверку не пройдёт, и токен до него не дойдёт. На боевом сервере
    проверено и обратное: неверное имя даёт CERTIFICATE_VERIFY_FAILED.
    """
    _settings(monkeypatch, tg_proxy_url=PROXY)
    seen = _network(monkeypatch, {IP: _ok, "прокси": _ok})

    asyncio.run(tg.call(TOKEN, "getMe"))

    [request] = seen[IP]
    assert request.url.host == IP
    assert request.headers["host"] == TELEGRAM_HOST
    assert request.extensions["sni_hostname"] == TELEGRAM_HOST
    assert seen["прокси"] == []


def test_ipv6_address_is_bracketed_in_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, tg_api_ips="2001:67c:4e8:f004::9")
    assert tg.routes()[0].origin == "https://[2001:67c:4e8:f004::9]"


@pytest.mark.parametrize("value", ["api.telegram.org", "evil.example", "149.154.167",
                                   "149.154.167.220, not-an-ip"])
def test_only_addresses_are_accepted_as_direct_routes(value: str) -> None:
    """Имя хоста здесь означало бы второй DNS — а закрыт как раз DNS-адрес."""
    with pytest.raises(ValidationError):
        Settings(**BASE, tg_api_ips=value)


def test_direct_addresses_are_parsed_from_a_list() -> None:
    s = Settings(**BASE, tg_api_ips=" 149.154.167.220 , 149.154.167.221,")
    assert s.tg_direct_ips == ("149.154.167.220", "149.154.167.221")
    assert Settings(**BASE, tg_api_ips="  ").tg_direct_ips == ()


# ------------------------------------------------------------ переключение
def test_closed_path_falls_over_within_the_same_call(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Соединение не установилось — запрос не ушёл, повтор другим путём безопасен."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    seen = _network(monkeypatch, {IP: _refused, "прокси": _ok})

    with caplog.at_level(logging.WARNING, logger=tg.log.name):
        result = asyncio.run(tg.call(TOKEN, "getMe"))

    assert result == {"username": "devon_sd_bot"}
    assert len(seen[IP]) == 1 and len(seen["прокси"]) == 1
    assert any(IP in r.getMessage() and "закрыт" in r.getMessage()
               for r in caplog.records)


def test_closed_path_is_remembered_and_not_retried_first(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Закрытый путь стоит секунды один раз, а не на каждом вызове."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    seen = _network(monkeypatch, {IP: _refused, "прокси": _ok})

    asyncio.run(tg.call(TOKEN, "getMe"))
    asyncio.run(tg.call(TOKEN, "getMe"))

    assert len(seen[IP]) == 1, "второй вызов обязан начаться с исправного пути"
    assert len(seen["прокси"]) == 2


def test_direct_route_is_tried_first_again_after_cooldown(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Прямой путь починили — возвращаемся на него сами, без перезапуска."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    seen = _network(monkeypatch, {IP: _ok, "прокси": _ok})
    tg._down_until[IP] = 0.0  # отказ был, но срок его давно истёк

    with caplog.at_level(logging.INFO, logger=tg.log.name):
        asyncio.run(tg.call(TOKEN, "getMe"))

    assert len(seen[IP]) == 1 and seen["прокси"] == []
    assert IP not in tg._down_until
    assert any("снова открыт" in r.getMessage() for r in caplog.records)


def test_read_timeout_is_not_repeated_on_another_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """После таймаута чтения запрос мог дойти: `sendMessage` повтором задвоился бы."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    seen = _network(monkeypatch, {IP: _frozen, "прокси": _ok})

    with pytest.raises(tg.TelegramError) as caught:
        asyncio.run(tg.send_message(TOKEN, 1, "текст"))

    assert caught.value.code == 0 and "ReadTimeout" in caught.value.description
    assert seen["прокси"] == [], "повтор другим путём — это второе сообщение в чате"


def test_every_path_closed_is_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Закрыто всё — внятная ошибка транспорта, а не молчание или AssertionError."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    seen = _network(monkeypatch, {IP: _refused, "прокси": _refused})

    with pytest.raises(tg.TelegramError) as caught:
        asyncio.run(tg.call(TOKEN, "getMe"))
    assert caught.value.code == 0 and "ConnectTimeout" in caught.value.description

    # Когда закрыто всё, порядок настройки сохраняется: прямой по-прежнему первый.
    with contextlib.suppress(tg.TelegramError):
        asyncio.run(tg.call(TOKEN, "getMe"))
    assert [len(seen[IP]), len(seen["прокси"])] == [2, 2]


def test_telegram_refusal_is_an_answer_not_a_closed_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Ответ Telegram с ошибкой — это ответ: путь исправен, повторять нечего."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    seen = _network(monkeypatch, {
        IP: lambda r: httpx.Response(400, json={"ok": False, "error_code": 400,
                                                "description": "Bad Request"}),
        "прокси": _ok})

    with pytest.raises(tg.TelegramError) as caught:
        asyncio.run(tg.call(TOKEN, "getMe"))
    assert caught.value.code == 400
    assert seen["прокси"] == [] and IP not in tg._down_until


# -------------------------------------------------------------------- файлы
def test_files_download_by_the_same_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Через прокси не скачивалась ни одна фотография: замерзание бьёт и сюда."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    seen = _network(monkeypatch, {IP: lambda r: httpx.Response(200, content=b"x" * 47088),
                                  "прокси": _ok})

    content = asyncio.run(tg.download_file(TOKEN, "stickers/file_1.webp"))

    assert len(content) == 47088
    [request] = seen[IP]
    assert request.method == "GET"
    assert request.url.path == f"/file/bot{TOKEN}/stickers/file_1.webp"
    assert request.extensions["sni_hostname"] == TELEGRAM_HOST


def test_failed_download_does_not_carry_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Токен лежит в адресе файла — в текст ошибки адрес попасть не должен (И-7)."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    _network(monkeypatch, {IP: lambda r: httpx.Response(404, text="Not Found"),
                           "прокси": _ok})

    with pytest.raises(tg.TelegramError) as caught:
        asyncio.run(tg.download_file(TOKEN, "photos/file_2.jpg"))
    assert caught.value.code == 404
    assert TOKEN not in str(caught.value) and "A" * 35 not in str(caught.value)


# ------------------------------------------------------------------ сроки
def test_deadline_covers_every_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Предел поллера строится от этого числа — оно обязано покрывать перебор путей."""
    _settings(monkeypatch, tg_proxy_url=PROXY, tg_api_ips=f"{IP},149.154.167.221")
    params = {"timeout": 25}
    assert tg.deadline("getUpdates", params) == (
        3 * tg.CONNECT_TIMEOUT + tg.http_timeout_for("getUpdates", params))
    assert tg.deadline("getMe", None) > tg.TIMEOUT


def test_connect_wait_is_shorter_than_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Закрытый путь должен стоить секунд, а не всего таймаута вызова."""
    _settings(monkeypatch, tg_proxy_url=PROXY)
    route = tg.routes()[0]
    client = tg._client(tg.http_timeout_for("getUpdates", {"timeout": 25}), route)
    try:
        assert client.timeout.connect == tg.CONNECT_TIMEOUT
        assert client.timeout.read == 40.0
    finally:
        asyncio.run(client.aclose())
