"""Привязка из Telegram: адрес согласия, обмен кода, чужой портал, перепривязка.

Проверяется то, чего не видно на экране, когда всё «получилось»:

* адрес экрана согласия собирается из `tenants.b24_domain`, а не из чего-то,
  что приехало снаружи (И-4);
* `state` одноразовый, чужого вида не принимает и живёт четверть часа —
  без этого чужой код связался бы с чьим угодно телеграм-аккаунтом;
* код, выданный ДРУГИМ порталом, не превращается в привязку к этому теннанту;
* перепривязка снимает прежнюю, а не оставляет две строки `authorized` при
  одном живом токене;
* личная ссылка не попадает в общий чат.

Часть тестов требует живой PostgreSQL (`TEST_DATABASE_URL`) — привязка это
запись в четыре таблицы, и подменять их значило бы проверять заглушки.
"""
from __future__ import annotations

import os
import types
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from b24bot.api import b24 as api_b24
from b24bot.b24 import oauth
from b24bot.bot import handlers, texts
from b24bot.domain import linking

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
PORTAL = "devondev.bitrix24.ru"


# ------------------------------------------------------- адрес экрана согласия
def test_authorize_url_is_built_from_portal_domain() -> None:
    url = oauth.authorize_url(PORTAL, "state-123")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == PORTAL
    assert parsed.path == oauth.AUTHORIZE_PATH
    assert query["response_type"] == ["code"]
    assert query["state"] == ["state-123"]
    assert query["client_id"] == ["test.client"]
    assert "redirect_uri" not in query, (
        "по умолчанию адрес возврата не передаётся: у локального приложения "
        "обработчик один, а незарегистрированный адрес портал вправе отвергнуть")


@pytest.mark.parametrize("domain", [
    "evil.example.com",
    "devondev.bitrix24.ru.evil.com",
    "devondev.bitrix24.ru/../x",
    "",
])
def test_authorize_url_refuses_foreign_domain(domain: str) -> None:
    with pytest.raises(ValueError, match="домен портала"):
        oauth.authorize_url(domain, "state")


def test_redirect_uri_appears_only_when_switched_on(monkeypatch: Any) -> None:
    def settings(*, on: bool) -> Any:
        return types.SimpleNamespace(
            b24_oauth_redirect=on, public_base_url="https://b24sdbot.devondev.ru",
            b24_client_id="test.client", b24_client_secret="test.secret")

    monkeypatch.setattr(oauth, "get_settings", lambda: settings(on=False))
    assert oauth.redirect_uri() is None

    monkeypatch.setattr(oauth, "get_settings", lambda: settings(on=True))
    assert oauth.redirect_uri() == "https://b24sdbot.devondev.ru" + oauth.CALLBACK_PATH

    url = oauth.authorize_url(PORTAL, "s", redirect_uri=oauth.redirect_uri())
    assert parse_qs(urlparse(url).query)["redirect_uri"] == [
        "https://b24sdbot.devondev.ru" + oauth.CALLBACK_PATH]


def test_redirect_uri_never_leaves_http(monkeypatch: Any) -> None:
    """На http адрес возврата не собирается: код уехал бы открытым текстом."""
    monkeypatch.setattr(oauth, "get_settings", lambda: types.SimpleNamespace(
        b24_oauth_redirect=True, public_base_url="http://localhost:8000"))
    assert oauth.redirect_uri() is None


# --------------------------------------------------------- опознание возврата
@pytest.mark.parametrize(("payload", "expected"), [
    ({"code": "c", "state": "s"}, True),
    ({"error": "access_denied", "state": "s"}, True),
    ({"code": "c"}, False),          # без state связать код не с чем
    ({"state": "s"}, False),         # без кода и ошибки это не возврат
    ({}, False),
    ({"DOMAIN": PORTAL, "member_id": "m"}, False),
])
def test_oauth_return_needs_both_state_and_outcome(payload: dict[str, str],
                                                   expected: bool) -> None:
    assert api_b24._is_oauth_return(payload) is expected


def test_refusal_pages_are_distinct_and_do_not_embed() -> None:
    """У отказов разные тексты: человек уже вошёл в портал, оракула тут нет.

    Зато встраиваться этой странице некуда — она открыта верхним уровнем в
    браузере, поэтому `frame-ancestors 'none'` и без скрипта портала.
    """
    seen = set()
    for code in ("state", "portal", "mismatch", "cancelled"):
        resp = api_b24._link_notice(code)
        body = resp.body.decode()
        assert resp.status_code == 200
        assert resp.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
        assert "api.bitrix24.com" not in body
        seen.add(api_b24.LINK_REFUSALS[code][0])
    assert len(seen) == 4


# ----------------------------------------------------------- ссылка в группе
def test_group_link_sends_to_private_and_never_shows_authorize_url() -> None:
    reply = handlers._link_to_private({"username": "devon_sd_bot", "tenant_id": 1})
    markup = reply.markup or {}
    urls = [b["url"] for row in markup["inline_keyboard"] for b in row]

    assert urls == [f"https://t.me/devon_sd_bot?start={linking.START_ARG}"]
    assert all("oauth/authorize" not in u for u in urls), (
        "личная ссылка в общем чате — это приглашение отдать свой доступ "
        "первому, кто нажмёт")
    assert reply.text == texts.MSG_LINK_IN_PRIVATE


def test_both_doors_write_through_one_function() -> None:
    """Обе двери обязаны писать привязку одним кодом.

    Страж по исходнику, а не по поведению: разъедься запись, оба пути всё равно
    показали бы «готово», и расхождение нашлось бы только по жалобе.
    """
    import inspect

    source = inspect.getsource(handlers._link_account)
    assert "linking.link_accounts" in source
    assert "INSERT INTO tenant_members" not in source


# ---------------------------------------------------------------- экранирование
async def test_notify_escapes_name_from_portal(monkeypatch: Any) -> None:
    """Имя приходит с портала, значит это чужой ввод (И-6)."""
    sent: list[str] = []

    async def fake_send(tenant_id: int, tg_user_id: int, text: str, *,
                        markup: Any = None, token: str | None = None) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(linking.dm, "bot_token", lambda tenant_id: _async("t"))
    monkeypatch.setattr(linking.dm, "send", fake_send)

    await linking.notify_linked(linking.Linked(
        tenant_id=1, b24_user_id=7, tg_user_id=42, portal_domain=PORTAL,
        display_name="<b>Пётр</b> <script>alert(1)</script>",
        replaced_tg_user_id=43))

    assert len(sent) == 2, "второму аккаунту тоже полагается знать, что доступ снят"
    assert "<script>" not in sent[0]
    assert "&lt;script&gt;" in sent[0]
    assert "<code>7</code>" in sent[1], (
        "у отобранной привязки в тексте назван пользователь портала: без него "
        "сообщение читается как «что-то сломалось»")


def _async(value: Any) -> Any:
    async def run() -> Any:
        return value
    return run()


# ============================================================ живая PostgreSQL
live = pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL")


async def _tenant(conn: Any, *, member: str | None = None) -> dict[str, Any]:
    uniq = uuid.uuid4().hex[:8]
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
        "VALUES ($1,$2,$3,$4,'active') RETURNING id",
        f"t{uniq}", "Теннант", member or f"member-{uniq}", PORTAL)
    return {"id": int(tenant_id), "member": member or f"member-{uniq}"}


def _portal(monkeypatch: Any, *, member: str, user_id: int = 7,
            token_user_id: int | None = None, error: str | None = None) -> None:
    """Портал: обмен кода и `user.current`. Сеть в тестах не участвует."""
    async def exchange(code: str) -> dict[str, Any] | None:
        if error:
            return {"error": error}
        return {"access_token": f"acc-{code}", "refresh_token": f"ref-{code}",
                "expires_in": 3600, "member_id": member,
                "user_id": token_user_id if token_user_id is not None else user_id}

    async def rest(domain: str, method: str, token: str,
                   params: Any = None) -> dict[str, Any]:
        assert method == "user.current"
        return {"result": {"ID": str(user_id), "NAME": "Пётр", "LAST_NAME": "Петров"}}

    async def admin(domain: str, token: str) -> bool:
        return False

    monkeypatch.setattr(oauth, "exchange_code", exchange)
    monkeypatch.setattr(oauth, "rest_call", rest)
    monkeypatch.setattr(oauth, "is_portal_admin", admin)


@live
async def test_begin_issues_single_use_state(db: Any) -> None:
    t = await _tenant(db)
    started = await linking.begin(t["id"], 4242)
    assert started is not None

    state = parse_qs(urlparse(started.url).query)["state"][0]
    row = await db.fetchrow(
        "SELECT kind, tenant_id, single_use, owner_tg_id FROM callback_tokens "
        "WHERE expires_at > now() ORDER BY created_at DESC LIMIT 1")
    assert row["kind"] == linking.OAUTH_STATE
    assert row["tenant_id"] == t["id"]
    assert row["single_use"] is True
    assert row["owner_tg_id"] is None, (
        "владельца у state нет намеренно: в браузере нажавшего не опознать, "
        "а проверка, которая всегда проходит, хуже отсутствующей")

    from b24bot.domain.context import consume_token
    assert await consume_token(state, None) is not None
    assert await consume_token(state, None) is None


@live
async def test_complete_links_account_and_stores_token(
        db: Any, monkeypatch: Any) -> None:
    t = await _tenant(db)
    _portal(monkeypatch, member=t["member"], user_id=7)

    started = await linking.begin(t["id"], 4242)
    assert started is not None
    state = parse_qs(urlparse(started.url).query)["state"][0]

    result = await linking.complete(state, "code-1", domain_hint=PORTAL,
                                    member_hint=t["member"])
    assert isinstance(result, linking.Linked)
    assert (result.b24_user_id, result.tg_user_id) == (7, 4242)
    assert result.display_name == "Пётр Петров"

    member = await db.fetchrow(
        "SELECT m.b24_user_id, m.link_status FROM tenant_members m "
        "JOIN users u ON u.id = m.user_id WHERE m.tenant_id = $1 "
        "AND u.tg_user_id = 4242", t["id"])
    assert (member["b24_user_id"], member["link_status"]) == (7, "authorized")

    token = await db.fetchrow(
        "SELECT state, role, authorized_tg_user_id FROM b24_user_tokens "
        "WHERE tenant_id = $1 AND b24_user_id = 7", t["id"])
    assert token["state"] == "active"
    assert token["role"] == "user"
    assert token["authorized_tg_user_id"] == 4242

    audit_row = await db.fetchrow(
        "SELECT action, high_risk, actor_tg_id FROM audit_log "
        "WHERE tenant_id = $1 AND action = 'user.map'", t["id"])
    assert audit_row is not None, "сопоставление человека — high_risk с самого начала"
    assert audit_row["high_risk"] is True
    assert audit_row["actor_tg_id"] == 4242


@live
async def test_state_works_once(db: Any, monkeypatch: Any) -> None:
    t = await _tenant(db)
    _portal(monkeypatch, member=t["member"])
    started = await linking.begin(t["id"], 4242)
    assert started is not None
    state = parse_qs(urlparse(started.url).query)["state"][0]

    assert isinstance(await linking.complete(state, "code-1"), linking.Linked)
    again = await linking.complete(state, "code-2")
    assert isinstance(again, linking.Refusal)
    assert again.code == "state"


@live
async def test_token_of_other_kind_is_not_a_state(db: Any,
                                                  monkeypatch: Any) -> None:
    """Токен меню под видом `state` — та же подмена, что и у кнопок."""
    t = await _tenant(db)
    _portal(monkeypatch, member=t["member"])

    from b24bot.domain.context import issue_token
    alien = await issue_token("menu", tenant_id=t["id"], payload={"tg_user_id": 1})

    result = await linking.complete(alien, "code-1")
    assert isinstance(result, linking.Refusal)
    assert result.code == "state"


@live
async def test_code_from_another_portal_is_refused(db: Any,
                                                   monkeypatch: Any) -> None:
    t = await _tenant(db)
    _portal(monkeypatch, member="member-of-someone-else")
    started = await linking.begin(t["id"], 4242)
    assert started is not None
    state = parse_qs(urlparse(started.url).query)["state"][0]

    result = await linking.complete(state, "code-1")
    assert isinstance(result, linking.Refusal)
    assert result.code == "mismatch"
    assert await db.fetchval(
        "SELECT count(*) FROM b24_user_tokens WHERE tenant_id = $1", t["id"]) == 0


@live
async def test_callback_domain_is_only_checked_never_trusted(
        db: Any, monkeypatch: Any) -> None:
    t = await _tenant(db)
    _portal(monkeypatch, member=t["member"])
    started = await linking.begin(t["id"], 4242)
    assert started is not None
    state = parse_qs(urlparse(started.url).query)["state"][0]

    result = await linking.complete(state, "code-1",
                                    domain_hint="attacker.bitrix24.ru")
    assert isinstance(result, linking.Refusal)
    assert result.code == "mismatch"


@live
async def test_two_sources_about_one_user_must_agree(
        db: Any, monkeypatch: Any) -> None:
    """`user_id` обмена и `user.current` разошлись — писать наугад нельзя."""
    t = await _tenant(db)
    _portal(monkeypatch, member=t["member"], user_id=7, token_user_id=9)
    started = await linking.begin(t["id"], 4242)
    assert started is not None
    state = parse_qs(urlparse(started.url).query)["state"][0]

    result = await linking.complete(state, "code-1")
    assert isinstance(result, linking.Refusal)
    assert result.code == "mismatch"
    assert await db.fetchval(
        "SELECT count(*) FROM tenant_members WHERE tenant_id = $1", t["id"]) == 0


@live
async def test_portal_error_writes_nothing(db: Any, monkeypatch: Any) -> None:
    t = await _tenant(db)
    _portal(monkeypatch, member=t["member"], error="invalid_grant")
    started = await linking.begin(t["id"], 4242)
    assert started is not None
    state = parse_qs(urlparse(started.url).query)["state"][0]

    result = await linking.complete(state, "code-1")
    assert isinstance(result, linking.Refusal)
    assert result.code == "portal"
    assert await db.fetchval(
        "SELECT count(*) FROM tenant_members WHERE tenant_id = $1", t["id"]) == 0


@live
async def test_relink_takes_the_account_from_previous_telegram(
        db: Any, monkeypatch: Any) -> None:
    """Один пользователь Битрикса — один живой телеграм.

    `b24_user_tokens` хранит ровно один `authorized_tg_user_id`; оставить при
    этом две строки `authorized` значит расписаться в состоянии, которого нет.
    """
    t = await _tenant(db)
    _portal(monkeypatch, member=t["member"], user_id=7)

    first = await linking.begin(t["id"], 100)
    assert first is not None
    await linking.complete(parse_qs(urlparse(first.url).query)["state"][0], "c1")

    second = await linking.begin(t["id"], 200)
    assert second is not None
    result = await linking.complete(
        parse_qs(urlparse(second.url).query)["state"][0], "c2")

    assert isinstance(result, linking.Linked)
    assert result.replaced_tg_user_id == 100

    rows = {r["tg_user_id"]: r["link_status"] for r in await db.fetch(
        "SELECT u.tg_user_id, m.link_status FROM tenant_members m "
        "JOIN users u ON u.id = m.user_id WHERE m.tenant_id = $1", t["id"])}
    assert rows == {100: "revoked", 200: "authorized"}
    assert await db.fetchval(
        "SELECT authorized_tg_user_id FROM b24_user_tokens "
        "WHERE tenant_id = $1 AND b24_user_id = 7", t["id"]) == 200

    from b24bot.domain import access
    assert await access.linked_b24_user(t["id"], 100) is None
    assert await access.linked_b24_user(t["id"], 200) == 7


@live
async def test_inactive_tenant_has_nothing_to_begin(db: Any) -> None:
    t = await _tenant(db)
    await db.execute("UPDATE tenants SET status = 'suspended' WHERE id = $1", t["id"])
    assert await linking.begin(t["id"], 4242) is None


@live
async def test_telegram_identity_travels_through_state(db: Any, monkeypatch: Any) -> None:
    """Обратно человек приходит в браузере, где от Telegram нет ничего.

    Имя и `@username` кладутся в `payload` при выдаче ссылки — иначе строка в
    `users` осталась бы безымянной, и экран «Команда» показывал бы пустые строки.
    """
    t = await _tenant(db)
    _portal(monkeypatch, member=t["member"], user_id=7)

    started = await linking.begin(t["id"], 4242, tg_username="ivanov",
                                  display_name="Иван Иванов (@ivanov)")
    assert started is not None
    state = parse_qs(urlparse(started.url).query)["state"][0]
    assert isinstance(await linking.complete(state, "code-1"), linking.Linked)

    row = await db.fetchrow(
        "SELECT tg_username, display_name FROM users WHERE tg_user_id = 4242")
    assert row["tg_username"] == "ivanov"
    assert row["display_name"] == "Иван Иванов (@ivanov)"
