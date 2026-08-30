"""Второй рубеж изоляции: политики RLS (миграция 0020) на настоящей PostgreSQL.

Первый рубеж — обязательный tenant_id в каждом запросе — проверяет
`tests/test_isolation.py`. Здесь проверяется страховка: что чужие строки не
видны, даже когда запрос про tenant_id ЗАБЫЛ. Ровно этот класс ошибок RLS и
ловит, поэтому центральный тест намеренно выполняет выборку без единого фильтра.

Все обращения к базе в этих тестах идут через фасад пула — тем же путём, что и
код приложения; прямое соединение фикстуры (`app.rls='off'`) используется только
для сборки мира и для контроля «что лежит в таблице на самом деле».
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import asyncpg
import pytest

from b24bot.db import pool as pool_mod
from b24bot.db.pool import pool, system_scope, tenant_scope

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]


async def _tenant(conn: Any) -> int:
    uniq = uuid.uuid4().hex[:8]
    tenant = await conn.fetchval(
        "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
        "VALUES ($1,$2,$3,$4,'active') RETURNING id",
        f"t{uniq}", "Теннант", f"member-{uniq}", f"{uniq}.bitrix24.ru")
    await conn.execute(
        "INSERT INTO task_cache (tenant_id, b24_task_id, is_ours, title) "
        "VALUES ($1, 100, true, 'секретная задача')", tenant)
    return int(tenant)


async def _fetchval(sql: str, *args: Any) -> Any:
    """Свежий захват через фасад — тем же путём, что ходит приложение."""
    async with pool().acquire() as conn:
        return await conn.fetchval(sql, *args)


# ------------------------------------------------------------------- страж
async def test_every_tenant_table_has_forced_rls_policy(db: Any) -> None:
    """Таблица с tenant_id без включённого FORCE RLS и политики — дыра.

    Список не перечислен руками: новая таблица обязана принести политику своей
    миграцией, и этот тест — то, что ей об этом напомнит.
    """
    rows = await db.fetch(
        """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
               (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
           AND (c.relname = 'tenants' OR EXISTS (
                SELECT 1 FROM pg_attribute a
                 WHERE a.attrelid = c.oid AND a.attname = 'tenant_id'
                   AND NOT a.attisdropped))
        """)
    assert rows, "обход таблиц пуст — сам тест сломан"
    bad = [r["relname"] for r in rows
           if not (r["relrowsecurity"] and r["relforcerowsecurity"]
                   and r["policies"] > 0)]
    assert not bad, f"таблицы без принудительной политики RLS: {bad}"


# ------------------------------------------------------------ забытый фильтр
async def test_forgotten_tenant_filter_is_contained(db: Any) -> None:
    """Выборка БЕЗ tenant_id под скоупом теннанта видит только его строки.

    Это и есть смысл второго рубежа: И-2 ловится стражем, но код с забытым
    фильтром до сих пор читал бы всё. Теперь — только своё.
    """
    a, b = await _tenant(db), await _tenant(db)

    with tenant_scope(a):
        seen = await _fetchval("SELECT count(*) FROM task_cache")
        own = await _fetchval(
            "SELECT tenant_id FROM task_cache LIMIT 1")
    assert seen == 1 and own == a, "скоуп теннанта обязан сузить и голую выборку"

    with tenant_scope(b):
        assert await _fetchval("SELECT count(*) FROM task_cache") == 1

    # Прямой канал фикстуры (обслуживание) видит обе строки — мир собран верно.
    total = await db.fetchval(
        "SELECT count(*) FROM task_cache WHERE tenant_id = ANY($1::bigint[])",
        [a, b])
    assert total == 2


async def test_tenants_row_is_visible_only_to_itself(db: Any) -> None:
    a, b = await _tenant(db), await _tenant(db)
    with tenant_scope(a):
        assert await _fetchval("SELECT count(*) FROM tenants WHERE id = $1", a) == 1
        assert await _fetchval("SELECT count(*) FROM tenants WHERE id = $1", b) == 0


async def test_unset_and_foreign_context_see_nothing(db: Any) -> None:
    """Пустой контекст — fail-closed: путь без объявления ломается громко."""
    await _tenant(db)

    token = pool_mod._ctx.set("")  # снять фоновый системный скоуп фикстуры
    try:
        assert await _fetchval("SELECT count(*) FROM task_cache") == 0
        assert await _fetchval("SELECT count(*) FROM tenants") == 0
    finally:
        pool_mod._ctx.reset(token)

    with tenant_scope(10**9):  # несуществующий теннант — то же самое
        assert await _fetchval("SELECT count(*) FROM task_cache") == 0


async def test_system_scope_sees_all_tenants(db: Any) -> None:
    a, b = await _tenant(db), await _tenant(db)
    with system_scope():
        both = await _fetchval(
            "SELECT count(*) FROM tenants WHERE id = ANY($1::bigint[])", [a, b])
    assert both == 2


# ------------------------------------------------------------------- запись
async def test_write_into_foreign_tenant_is_rejected(db: Any) -> None:
    """WITH CHECK: под скоупом одного теннанта строку другому не вставить."""
    a, b = await _tenant(db), await _tenant(db)

    with tenant_scope(a):
        async with pool().acquire() as conn:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO clients (tenant_id, name, status) "
                    "VALUES ($1, 'чужой клиент', 'active')", b)
            await conn.execute(
                "INSERT INTO clients (tenant_id, name, status) "
                "VALUES ($1, 'свой клиент', 'active')", a)

    assert await db.fetchval(
        "SELECT count(*) FROM clients WHERE tenant_id = $1", b) == 0


async def test_shared_null_tenant_rows_stay_shared(db: Any) -> None:
    """Незаявленный чат ничей — виден и создаётся из любого скоупа.

    Это семантика регистрации чатов (`dispatch.handle`): чат появляется до
    привязки, и политика с веткой `tenant_id IS NULL` обязана его пропустить.
    """
    a = await _tenant(db)
    chat_id = -int(uuid.uuid4().int % 10**9)

    with tenant_scope(a):
        async with pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO tg_chats (chat_id, tenant_id, type, title, status) "
                "VALUES ($1, NULL, 'supergroup', 'новый чат', 'unclaimed')", chat_id)
        assert await _fetchval(
            "SELECT count(*) FROM tg_chats WHERE chat_id = $1", chat_id) == 1


# ------------------------------------------------------------------ гигиена
async def test_released_connection_does_not_leak_scope(db: Any) -> None:
    """Скоуп не переживает возврат соединения в пул.

    Пул мал, соединения переиспользуются; следующий захват обязан переобъявить
    контекст, а не унаследовать чужой.
    """
    a = await _tenant(db)

    with tenant_scope(a):
        assert await _fetchval("SELECT count(*) FROM tenants") == 1

    token = pool_mod._ctx.set("")
    try:
        # То же самое физическое соединение из пула — но контекст уже пуст.
        for _ in range(8):
            assert await _fetchval("SELECT count(*) FROM tenants") == 0
    finally:
        pool_mod._ctx.reset(token)


async def test_maintenance_connection_bypasses_policies(db: Any) -> None:
    """Соединение с `app.rls='off'` (фикстура, alembic, psql) политикам не
    подчиняется — иначе ни миграции, ни ручная диагностика не работали бы."""
    a, b = await _tenant(db), await _tenant(db)
    assert await db.fetchval(
        "SELECT count(*) FROM tenants WHERE id = ANY($1::bigint[])", [a, b]) == 2


# --------------------------------------------------------------- точки входа
async def test_entry_points_declare_scopes() -> None:
    """Страж обвязки: резолверы недоверенного ввода — системные, обработка —
    теннантская. Проверяется по исходнику, как стражи команд и колбэков."""
    import inspect

    from b24bot.api import app_ui, tg_webhook
    from b24bot.api import b24 as b24_api
    from b24bot.bot import dispatch, poller
    from b24bot.domain import linking, miniapp
    from b24bot.worker import main as worker

    for fn, needle in [
        (app_ui.load_session, "system_scope"),
        (app_ui.load_session, "set_tenant"),
        (b24_api.placement, "set_tenant"),
        (b24_api.events, "set_tenant"),
        (b24_api._app_event, "tenant_scope"),
        (b24_api.install, "set_tenant"),
        (tg_webhook.receive, "system_scope"),
        (dispatch.route, "set_tenant"),
        (dispatch.handle, "system_scope"),
        (poller.PollerRegistry.sync, "system_scope"),
        (linking.complete, "set_tenant"),
        (miniapp.authenticate, "system_scope"),
        (worker.process_events, "tenant_scope"),
        (worker.send_outbox, "tenant_scope"),
        (worker.flush_digests, "tenant_scope"),
    ]:
        src = inspect.getsource(fn)
        assert needle in src, f"{fn.__qualname__} потерял {needle}"

    from b24bot.api import miniapp as api_miniapp
    assert "set_tenant" in inspect.getsource(api_miniapp.actor_dep)
