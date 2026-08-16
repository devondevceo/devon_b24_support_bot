"""Клиент Битрикса: ретраи на лимитах, batch, keyset, классификация ошибок."""
from __future__ import annotations

import httpx
import pytest

from b24bot.b24 import errors
from b24bot.b24.client import B24Client
from b24bot.b24.limiter import Lane, PortalLimiter
from tests.fake_portal import FakePortal


def make_client(portal: FakePortal, limiter: PortalLimiter | None = None) -> B24Client:
    http = httpx.AsyncClient(transport=portal.transport, timeout=5)
    lim = limiter or PortalLimiter(rate=1000, capacity=1000)  # лимитер отдельно тестируем
    return B24Client("devondev.bitrix24.ru", lambda: _token(), lim, http=http)


async def _token() -> str:
    return "fake-access-token"


@pytest.mark.asyncio
async def test_call_returns_result_and_reads_time_block() -> None:
    portal = FakePortal(operating=42.0)
    limiter = PortalLimiter(rate=1000, capacity=1000)
    async with make_client(portal, limiter) as c:
        res = await c.call("user.current")
    assert res["ID"] == "1"
    # Бюджет operating обязан обновляться из каждого ответа, иначе вторая ось лимита
    # существует только на бумаге.
    assert limiter.stats["operating_spent"] == 42.0


@pytest.mark.asyncio
async def test_query_limit_is_retried_and_succeeds() -> None:
    portal = FakePortal(fail_times=2, fail_code="QUERY_LIMIT_EXCEEDED", fail_status=503)
    async with make_client(portal) as c:
        res = await c.call("user.current")
    assert res["ID"] == "1"
    assert portal.method_calls("user.current") == 3


@pytest.mark.asyncio
async def test_auth_error_is_not_retried() -> None:
    """Ретрай мёртвым токеном бессмыслен: нужен refresh, а не повтор."""
    portal = FakePortal(fail_times=5, fail_code="expired_token", fail_status=401)
    async with make_client(portal) as c:
        with pytest.raises(errors.B24AuthError):
            await c.call("user.current")
    assert portal.method_calls("user.current") == 1


@pytest.mark.asyncio
async def test_access_denied_is_not_retried() -> None:
    portal = FakePortal(fail_times=5, fail_code="ACCESS_DENIED", fail_status=403)
    async with make_client(portal) as c:
        with pytest.raises(errors.B24AccessDenied):
            await c.call("tasks.task.get", {"taskId": 1})
    assert portal.method_calls("tasks.task.get") == 1


@pytest.mark.asyncio
async def test_batch_packs_commands_and_reports_partial_errors() -> None:
    portal = FakePortal()
    async with make_client(portal) as c:
        out = await c.batch({
            "a": ("tasks.task.get", {"taskId": 199}),
            "b": ("unknown.method", {}),
        })
    assert out["result"]["a"]["task"]["id"] == "199"
    # Ошибка одной команды не роняет остальные — это и есть смысл batch.
    assert "b" in out["errors"]
    assert portal.method_calls("batch") == 1


@pytest.mark.asyncio
async def test_batch_rejects_more_than_50_commands() -> None:
    portal = FakePortal()
    async with make_client(portal) as c:
        with pytest.raises(ValueError):
            await c.batch({str(i): ("user.current", {}) for i in range(51)})


@pytest.mark.asyncio
async def test_list_all_walks_keyset_until_short_page() -> None:
    portal = FakePortal(tasks=120)
    async with make_client(portal) as c:
        items = await c.list_all("tasks.task.list", {"filter": {"GROUP_ID": 33}},
                                 items_key="tasks", lane=Lane.BACKGROUND)
    assert len(items) == 120
    assert len({i["id"] for i in items}) == 120
    # 50 + 50 + 20: третья страница короче лимита и обход прекращается.
    assert portal.method_calls("tasks.task.list") == 3
    # start=-1 обязан уходить в каждом запросе: он отключает COUNT.
    assert all(params.get("start") == "-1" for m, params in portal.calls
               if m == "tasks.task.list")


@pytest.mark.asyncio
async def test_client_refuses_untrusted_domain() -> None:
    """Инвариант И-4: адрес портала не может прийти из данных."""
    with pytest.raises(ValueError):
        B24Client("evil.example.com", _token, PortalLimiter())
