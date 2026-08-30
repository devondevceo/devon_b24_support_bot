"""Жизненный цикл тиражного приложения: деинсталляция, подписка, чистка, журнал.

Что здесь дорого и почему проверяется на настоящей PostgreSQL:

* **Поддельный `ONAPPUNINSTALL` (И-8).** Событие с валидным APPLICATION_TOKEN
  может отправить любой сотрудник портала. Пропустить его — значит остановить
  бота теннанта и через 30 дней стереть его данные. Подлинность доказывает
  только смерть НАШЕГО токена.
* **Чистка данных.** `purge_due` обязана стереть теннанта из КАЖДОЙ таблицы с
  `tenant_id` — включая будущие. Проверка обходит information_schema, а не
  перечисляет таблицы руками: новая таблица без каскада провалит тест, а не
  переживёт чистку молча.
* **`license_blocked`.** Правило существует в двух местах (генерированная
  колонка и `lifecycle.is_blocked`) — тест сверяет их друг с другом на всех
  статусах, включая NULL, где `IN` без COALESCE молча дал бы NULL.

Чистые тесты (разбор app.info, правило блокировки, наблюдатель клиента,
согласованность срока чистки с юрдокументами) живут в этом же файле ниже —
им база не нужна.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from b24bot.b24 import errors
from b24bot.b24.tokens import NeedsReauth
from b24bot.crypto import box
from b24bot.domain import lifecycle

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")

live = pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL")

pytestmark = [pytest.mark.asyncio]


async def _tenant(conn: Any, **cols: Any) -> int:
    uniq = uuid.uuid4().hex[:8]
    tenant = await conn.fetchval(
        "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
        "VALUES ($1,$2,$3,$4,'active') RETURNING id",
        f"t{uniq}", "Теннант", f"member-{uniq}", f"{uniq}.bitrix24.ru")
    for key, value in cols.items():
        await conn.execute(f"UPDATE tenants SET {key} = $2 WHERE id = $1",  # noqa: S608
                           tenant, value)
    return int(tenant)


async def _service_token(conn: Any, tenant: int) -> None:
    a = box.encrypt("access", box.aad("b24_user_tokens", "access_token", tenant, 1))
    r = box.encrypt("refresh", box.aad("b24_user_tokens", "refresh_token", tenant, 1))
    await conn.execute(
        "INSERT INTO b24_user_tokens (tenant_id, b24_user_id, role, access_token, "
        "refresh_token, enc_kid, expires_at, state) "
        "VALUES ($1,1,'service_admin',$2,$3,$4,now() + interval '1 hour','active')",
        tenant, a, r, box.kid_of(a))


def _info(status: str = "L", expired: bool = False,
          version: int | None = 1) -> lifecycle.AppInfo:
    return lifecycle.AppInfo(status=status, payment_expired=expired,
                             version=version, days=None)


# ------------------------------------------------------------- ONAPPUNINSTALL
@live
async def test_fake_uninstall_leaves_tenant_untouched(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """И-8: app.info живым токеном отвечает — событие поддельное, ничего не трогаем."""
    tenant = await _tenant(db)

    async def _alive(tenant_id: int) -> lifecycle.AppInfo:
        return _info()

    monkeypatch.setattr(lifecycle, "_fetch_app_info", _alive)
    assert await lifecycle.on_uninstall_event(tenant) is False

    row = await db.fetchrow(
        "SELECT status, uninstalled_at FROM tenants WHERE id = $1", tenant)
    assert row["status"] == "active" and row["uninstalled_at"] is None


@live
async def test_confirmed_uninstall_marks_revokes_and_audits(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = await _tenant(db)
    await _service_token(db, tenant)

    async def _dead(tenant_id: int) -> lifecycle.AppInfo:
        raise NeedsReauth(tenant_id, 1, "invalid_grant")

    monkeypatch.setattr(lifecycle, "_fetch_app_info", _dead)
    assert await lifecycle.on_uninstall_event(tenant) is True

    row = await db.fetchrow(
        "SELECT status, uninstalled_at FROM tenants WHERE id = $1", tenant)
    assert row["status"] == "uninstalled" and row["uninstalled_at"] is not None

    token_state = await db.fetchval(
        "SELECT state FROM b24_user_tokens WHERE tenant_id = $1", tenant)
    assert token_state == "revoked"

    audit = await db.fetchrow(
        "SELECT action, high_risk FROM audit_log WHERE tenant_id = $1 "
        "ORDER BY occurred_at DESC LIMIT 1", tenant)
    assert audit is not None and audit["action"] == "tenant.uninstall"
    assert audit["high_risk"], "остановка теннанта обязана быть high_risk"


@live
async def test_inconclusive_uninstall_does_nothing(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Сеть легла — это не подтверждение. Ложная деинсталляция дороже поздней."""
    tenant = await _tenant(db)

    async def _flaky(tenant_id: int) -> lifecycle.AppInfo:
        raise errors.B24Transport("TRANSPORT", "timeout", "app.info")

    monkeypatch.setattr(lifecycle, "_fetch_app_info", _flaky)
    assert await lifecycle.on_uninstall_event(tenant) is False
    assert await db.fetchval(
        "SELECT status FROM tenants WHERE id = $1", tenant) == "active"


@live
async def test_reinstall_reactivates_marked_tenant(db: Any) -> None:
    """Переустановка в окне хранения возвращает теннанта как был."""
    from b24bot.api import b24 as b24_api

    tenant = await _tenant(db)
    domain = await db.fetchval("SELECT b24_domain FROM tenants WHERE id = $1", tenant)
    member = await db.fetchval("SELECT b24_member_id FROM tenants WHERE id = $1",
                               tenant)
    await db.execute(
        "UPDATE tenants SET status = 'uninstalled', uninstalled_at = now() "
        "WHERE id = $1", tenant)

    n: dict[str, str | None] = {"member_id": member, "domain": domain,
                                "app_token": None}
    again = await b24_api.upsert_tenant(n, None)
    assert again == tenant

    row = await db.fetchrow(
        "SELECT status, uninstalled_at FROM tenants WHERE id = $1", tenant)
    assert row["status"] == "active" and row["uninstalled_at"] is None


# ---------------------------------------------------------------- ONAPPUPDATE
@live
async def test_update_event_with_same_version_is_forged(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Версия не менялась — токен из события НЕ принимается (И-8)."""
    tenant = await _tenant(db, b24_app_version=5)

    async def _same(tenant_id: int) -> lifecycle.AppInfo:
        return _info(version=5)

    monkeypatch.setattr(lifecycle, "_fetch_app_info", _same)
    assert await lifecycle.on_update_event(tenant, "attacker-token", None) is False
    assert await db.fetchval(
        "SELECT b24_app_token FROM tenants WHERE id = $1", tenant) is None


@live
async def test_update_event_with_new_version_rotates_token_and_rebinds(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = await _tenant(db, b24_app_version=5)
    member = await db.fetchval("SELECT b24_member_id FROM tenants WHERE id = $1",
                               tenant)
    rebound: list[int] = []

    async def _newer(tenant_id: int) -> lifecycle.AppInfo:
        return _info(status="S", version=6)

    async def _rebind(tenant_id: int) -> tuple[int, int]:
        rebound.append(tenant_id)
        return 0, len(lifecycle.REQUIRED_EVENTS)

    monkeypatch.setattr(lifecycle, "_fetch_app_info", _newer)
    monkeypatch.setattr(lifecycle, "ensure_event_bindings", _rebind)

    assert await lifecycle.on_update_event(tenant, "new-token", "task,im") is True
    assert rebound == [tenant], "после обновления подписки пересоздаются"

    row = await db.fetchrow(
        "SELECT b24_app_token, b24_app_version, granted_scope FROM tenants "
        "WHERE id = $1", tenant)
    assert row["b24_app_version"] == 6
    assert list(row["granted_scope"]) == ["task", "im"]
    plain = box.decrypt(row["b24_app_token"],
                        box.aad("tenants", "b24_app_token", tenant, str(member)))
    assert plain == "new-token"


# ------------------------------------------------------------ подписка Маркета
@live
async def test_license_blocked_column_matches_python_rule(db: Any) -> None:
    """Одно правило в двух местах — сверяем колонку с `is_blocked` на всех статусах."""
    for status in (None, "L", "F", "D", "T", "P", "S"):
        for expired in (False, True):
            tenant = await _tenant(db)
            await db.execute(
                "UPDATE tenants SET b24_app_status = $2, license_expired = $3 "
                "WHERE id = $1", tenant, status, expired)
            column = await db.fetchval(
                "SELECT license_blocked FROM tenants WHERE id = $1", tenant)
            assert column == lifecycle.is_blocked(status, expired), \
                f"расхождение SQL и Python: status={status} expired={expired}"


@live
async def test_refresh_license_writes_snapshot_and_blocks(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = await _tenant(db)

    async def _expired(tenant_id: int) -> lifecycle.AppInfo:
        return _info(status="S", expired=True, version=3)

    monkeypatch.setattr(lifecycle, "_fetch_app_info", _expired)
    info = await lifecycle.refresh_license(tenant)
    assert info.payment_expired

    row = await db.fetchrow(
        "SELECT b24_app_status, license_expired, b24_app_version, "
        "license_checked_at, license_blocked FROM tenants WHERE id = $1", tenant)
    assert row["b24_app_status"] == "S" and row["license_expired"]
    assert row["b24_app_version"] == 3 and row["license_checked_at"] is not None
    assert row["license_blocked"]
    assert await lifecycle.blocked(tenant) is True


@live
async def test_note_app_status_takes_only_known_letters(db: Any) -> None:
    """Буква из POST-а — чужой ввод: пишется только известный статус, и только он."""
    tenant = await _tenant(db)
    await lifecycle.note_app_status(tenant, "  p ")
    assert await db.fetchval(
        "SELECT b24_app_status FROM tenants WHERE id = $1", tenant) == "P"
    assert await db.fetchval(
        "SELECT license_checked_at FROM tenants WHERE id = $1", tenant) is None, \
        "буква из POST-а — не проверка; отметка ставится только по app.info"

    await lifecycle.note_app_status(tenant, "<script>")
    assert await db.fetchval(
        "SELECT b24_app_status FROM tenants WHERE id = $1", tenant) == "P"


# --------------------------------------------------------------------- чистка
@live
async def test_purge_wipes_tenant_from_every_table(db: Any) -> None:
    """После чистки ни одна таблица с tenant_id не держит строк теннанта.

    Обход по information_schema, а не по списку: таблица, добавленная позже без
    каскада от tenants, провалит этот тест, а не переживёт чистку молча.
    """
    doomed = await _tenant(db)
    survivor = await _tenant(db)
    for tenant in (doomed, survivor):
        await _service_token(db, tenant)
        await db.execute(
            "INSERT INTO audit_log (tenant_id, actor_kind, action, detail, high_risk) "
            "VALUES ($1,'system','test','{}',false)", tenant)
        await db.execute(
            "INSERT INTO b24_call_log (tenant_id, method, ok, duration_ms) "
            "VALUES ($1,'app.info',true,10)", tenant)
    await db.execute(
        "UPDATE tenants SET status = 'uninstalled', "
        "uninstalled_at = now() - interval '31 days' WHERE id = $1", doomed)

    assert await lifecycle.purge_due() == 1

    tables = await db.fetch(
        """
        SELECT c.table_name
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema AND t.table_name = c.table_name
         WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
           AND t.table_type = 'BASE TABLE'
        """)
    assert tables, "обход таблиц пуст — сам тест сломан"
    for row in tables:
        left = await db.fetchval(
            f'SELECT count(*) FROM "{row["table_name"]}" WHERE tenant_id = $1',  # noqa: S608
            doomed)
        assert left == 0, f"чистка не тронула {row['table_name']}"

    assert await db.fetchval(
        "SELECT count(*) FROM tenants WHERE id = $1", doomed) == 0
    assert await db.fetchval(
        "SELECT status FROM tenants WHERE id = $1", survivor) == "active", \
        "чистка одного теннанта не имеет права трогать соседний"
    assert await db.fetchval(
        "SELECT count(*) FROM b24_user_tokens WHERE tenant_id = $1", survivor) == 1


@live
async def test_purge_respects_reinstall_window(db: Any) -> None:
    tenant = await _tenant(db)
    await db.execute(
        "UPDATE tenants SET status = 'uninstalled', "
        "uninstalled_at = now() - interval '1 day' WHERE id = $1", tenant)
    await lifecycle.purge_due()
    assert await db.fetchval(
        "SELECT count(*) FROM tenants WHERE id = $1", tenant) == 1, \
        "окно на переустановку ещё открыто — данные трогать нельзя"


# --------------------------------------------------- журнал вызовов и ретенция
@live
async def test_call_log_observer_writes_and_cleanup_keeps_three_days(db: Any) -> None:
    from b24bot.domain.access import _call_logger
    from b24bot.worker import main as worker

    tenant = await _tenant(db)
    observe = _call_logger(tenant)
    await observe("tasks.task.get", True, None, 42)
    await observe("tasks.task.add", False, "QUERY_LIMIT_EXCEEDED", 1500)
    await db.execute(
        "INSERT INTO b24_call_log (tenant_id, method, ok, duration_ms, at) "
        "VALUES ($1,'old.call',true,5,now() - interval '4 days')", tenant)

    await worker.cleanup()

    rows = await db.fetch(
        "SELECT method, ok, error_code FROM b24_call_log WHERE tenant_id = $1 "
        "ORDER BY id", tenant)
    assert [r["method"] for r in rows] == ["tasks.task.get", "tasks.task.add"], \
        "строка старше 3 суток обязана исчезнуть, свежие — остаться"
    assert rows[1]["error_code"] == "QUERY_LIMIT_EXCEEDED"


# ------------------------------------------------------- смена домена портала
@live
async def test_refresh_payload_renames_portal_domain(db: Any) -> None:
    from b24bot.b24.tokens import _note_portal_domain

    tenant = await _tenant(db)
    member = await db.fetchval("SELECT b24_member_id FROM tenants WHERE id = $1",
                               tenant)

    # Чужой member_id ничего не переписывает.
    await _note_portal_domain(db, tenant, {"domain": "evil.bitrix24.ru",
                                           "member_id": "someone-else"})
    # Недоверенный домен — тоже.
    await _note_portal_domain(db, tenant, {"domain": "evil.example.com",
                                           "member_id": member})
    unchanged = await db.fetchval("SELECT b24_domain FROM tenants WHERE id = $1",
                                  tenant)
    assert unchanged.endswith(".bitrix24.ru") and "evil" not in unchanged

    await _note_portal_domain(db, tenant, {"domain": "renamed.bitrix24.ru",
                                           "member_id": member})
    assert await db.fetchval(
        "SELECT b24_domain FROM tenants WHERE id = $1", tenant) == "renamed.bitrix24.ru"


# ----------------------------------------------------- подписки на события
class _FakeClient:
    def __init__(self, bound: list[dict[str, str]]) -> None:
        self.bound = bound
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   **kw: Any) -> Any:
        self.calls.append((method, params))
        if method == "event.get":
            return self.bound
        return {}


async def test_ensure_event_bindings_binds_only_missing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    handler = lifecycle.events_handler_url()
    fake = _FakeClient([
        {"event": "ONTASKADD", "handler": handler},
        # Подписка нашего приложения, но на ЧУЖОЙ адрес (домен меняли) — не наша.
        {"event": "ONTASKUPDATE", "handler": "https://old.example/b24/events"},
    ])

    async def _client(tenant_id: int) -> _FakeClient:
        return fake

    monkeypatch.setattr(lifecycle.access, "client_for_service", _client)
    created, total = await lifecycle.ensure_event_bindings(1)

    assert total == len(lifecycle.REQUIRED_EVENTS)
    assert created == total - 1, "уже подписанный ONTASKADD не пересоздаётся"
    bound_now = {p["event"] for m, p in fake.calls if m == "event.bind" and p}
    assert "ONTASKADD" not in bound_now
    assert {"ONTASKUPDATE", "ONAPPUNINSTALL", "ONAPPUPDATE"} <= bound_now


async def test_required_events_are_the_eight_from_the_pilot() -> None:
    """Шесть событий задач + два жизненного цикла — те самые «восемь подписок»."""
    tasks = {e for e in lifecycle.REQUIRED_EVENTS if e.startswith("ONTASK")}
    apps = set(lifecycle.REQUIRED_EVENTS) - tasks
    assert len(tasks) == 6
    assert apps == {"ONAPPUNINSTALL", "ONAPPUPDATE"}


async def test_install_handler_binds_events_and_checks_license() -> None:
    """Страж: установка обязана подписываться на события и снимать лицензию.

    Ровно эта дыра и была: на пилоте подписки создали руками при спайке, и
    установка на любой новый портал не дала бы ни одного уведомления.
    """
    import inspect

    from b24bot.api import b24 as b24_api

    src = inspect.getsource(b24_api.install)
    assert "ensure_event_bindings" in src
    assert "refresh_license" in src


async def test_miniapp_gate_lives_in_the_single_entry() -> None:
    """Страж: гейт подписки стоит в единственной точке входа мини-аппа."""
    import inspect

    from b24bot.api import miniapp as miniapp_api

    assert "lifecycle.blocked" in inspect.getsource(miniapp_api.actor_dep)


# ------------------------------------------------------------- чистые правила
async def test_parse_app_info_is_liberal_about_types() -> None:
    for raw, expected in [
        ({"STATUS": "s", "PAYMENT_EXPIRED": "Y", "VERSION": "7"},
         ("S", True, 7)),
        ({"STATUS": "P", "PAYMENT_EXPIRED": True, "VERSION": 2},
         ("P", True, 2)),
        ({"STATUS": "L", "PAYMENT_EXPIRED": "N"}, ("L", False, None)),
        ({"STATUS": None, "PAYMENT_EXPIRED": None, "VERSION": "мусор"},
         ("", False, None)),
        ("не словарь вовсе", ("", False, None)),
    ]:
        info = lifecycle.parse_app_info(raw)
        assert (info.status, info.payment_expired, info.version) == expected, raw


async def test_blocking_rule_never_touches_free_and_local() -> None:
    for status in ("L", "F", "D", "", None):
        assert not lifecycle.is_blocked(status, True), \
            f"статус {status!r} не блокируется даже с PAYMENT_EXPIRED"
    for status in ("T", "P", "S"):
        assert lifecycle.is_blocked(status, True)
        assert not lifecycle.is_blocked(status, False)


async def test_purge_window_matches_legal_documents() -> None:
    """Срок в коде обещан пользователю юрдокументами — им нельзя разъезжаться."""
    from b24bot.api import legal

    assert lifecycle.PURGE_AFTER.days == 30
    for body in (legal.LICENSE_BODY, legal.PRIVACY_BODY, legal.DPA_BODY):
        assert "30 календарных дней" in body


async def test_legal_pages_name_the_app_and_the_vendor() -> None:
    """Модерация сверяет название решения и лицензиара с карточкой дословно."""
    from b24bot.api import legal

    assert legal.APP_NAME == "Поддержка в Telegram"
    for body in (legal.LICENSE_BODY, legal.PRIVACY_BODY, legal.DPA_BODY):
        assert legal.APP_NAME in body
    assert "Крищунс Кристина Владимировна" in legal.VENDOR_HTML
    assert "164303355280" in legal.VENDOR_HTML          # ИНН
    assert "324470400104481" in legal.VENDOR_HTML       # ОГРНИП


async def test_client_observer_sees_outcome_and_never_breaks_call(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from b24bot.b24.client import B24Client
    from b24bot.b24.limiter import LimiterRegistry

    seen: list[tuple[str, bool, str | None]] = []

    async def observe(method: str, ok: bool, code: str | None, ms: int) -> None:
        seen.append((method, ok, code))
        raise RuntimeError("наблюдатель упал — вызов не должен этого заметить")

    async def token() -> str:
        return "t"

    client = B24Client("x.bitrix24.ru", token, LimiterRegistry().for_tenant(1),
                       observer=observe)

    async def _ok(method: str, params: Any = None, **kw: Any) -> Any:
        return {"result": 1}

    async def _fail(method: str, params: Any = None, **kw: Any) -> Any:
        raise errors.B24AuthError("expired_token", "", method)

    monkeypatch.setattr(client, "_call_envelope", _ok)
    assert await client.call_envelope("app.info") == {"result": 1}

    monkeypatch.setattr(client, "_call_envelope", _fail)
    with pytest.raises(errors.B24AuthError):
        await client.call_envelope("tasks.task.get")

    assert seen == [("app.info", True, None),
                    ("tasks.task.get", False, "expired_token")]


async def test_blocked_bot_answers_commands_and_ignores_chatter(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Молчащий бот неотличим от сломанного — на команду отвечаем, на болтовню нет."""
    from b24bot.bot import dispatch, texts
    from b24bot.tg import api as tg

    sent: list[str] = []

    async def _bot_row(bot_ref: int) -> dict[str, Any]:
        return {"id": 1, "tenant_id": 7, "bot_id": 1, "username": "b", "token": "t"}

    async def _blocked(tenant_id: int) -> bool:
        return True

    async def _send(token: str, chat_id: int, text: str, **kw: Any) -> dict[str, Any]:
        sent.append(text)
        return {"message_id": 1}

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("заблокированный теннант не должен доходить до сценариев")

    monkeypatch.setattr(dispatch, "_bot_row", _bot_row)
    monkeypatch.setattr(lifecycle, "blocked", _blocked)
    monkeypatch.setattr(tg, "send_message", _send)
    monkeypatch.setattr(dispatch.handlers, "on_message", _boom)
    monkeypatch.setattr(dispatch.handlers, "on_callback", _boom)

    chat = {"id": -100, "type": "supergroup"}
    await dispatch.route(1, {"message": {"chat": chat, "text": "просто реплика"}})
    assert sent == [], "обычная переписка не повод напоминать о подписке"

    await dispatch.route(1, {"message": {
        "chat": chat, "text": "/task",
        "entities": [{"type": "bot_command", "offset": 0, "length": 5}]}})
    assert sent == [texts.MSG_LICENSE_EXPIRED]
