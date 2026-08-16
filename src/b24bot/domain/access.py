"""Построение клиента Битрикса под конкретную роль.

Кто чьим токеном ходит — docs/40-security.md §1:
  * действия человека — его личным токеном, права режет Битрикс;
  * фон (синхронизация, события) — токеном установщика приложения.

Сервисной учётки с урезанной видимостью нет (решение заказчика 16.08.2026), поэтому
область видимости задаёт ТОЛЬКО наш фильтр по группам. Ни одна выборка задач не
имеет права уйти в портал без списка разрешённых `GROUP_ID`.
"""
from __future__ import annotations

import logging

from b24bot.b24.client import B24Client
from b24bot.b24.limiter import LimiterRegistry
from b24bot.b24.tokens import NeedsReauth, TokenStore
from b24bot.db.pool import pool

log = logging.getLogger(__name__)

_limiters = LimiterRegistry()


async def _domain(tenant_id: int) -> str:
    async with pool().acquire() as conn:
        domain = await conn.fetchval(
            "SELECT b24_domain FROM tenants WHERE id = $1 AND status = 'active'", tenant_id)
    if not domain:
        raise NeedsReauth(tenant_id, 0, "теннант не активен")
    return str(domain)


async def client_for_user(tenant_id: int, b24_user_id: int, *,
                          actor_tg_user_id: int | None = None) -> B24Client:
    """Клиент от имени конкретного человека."""
    store = TokenStore(pool())
    domain = await _domain(tenant_id)

    async def provider() -> str:
        return await store.get_access_token(tenant_id, b24_user_id,
                                            actor_tg_user_id=actor_tg_user_id)

    return B24Client(domain, provider, _limiters.for_tenant(tenant_id))


async def client_for_service(tenant_id: int) -> B24Client:
    """Клиент для фоновых операций: токен установщика приложения."""
    async with pool().acquire() as conn:
        b24_user_id = await conn.fetchval(
            "SELECT b24_user_id FROM b24_user_tokens "
            "WHERE tenant_id = $1 AND role = 'service_admin' AND state = 'active' "
            "ORDER BY last_refresh_at DESC NULLS LAST LIMIT 1", tenant_id)
    if b24_user_id is None:
        raise NeedsReauth(tenant_id, 0, "нет живого сервисного токена")

    store = TokenStore(pool())
    domain = await _domain(tenant_id)

    async def provider() -> str:
        return await store.get_access_token(tenant_id, int(b24_user_id))

    return B24Client(domain, provider, _limiters.for_tenant(tenant_id))


async def linked_b24_user(tenant_id: int, tg_user_id: int) -> int | None:
    """ID пользователя Битрикса, если этот телеграм-аккаунт авторизован.

    `matched` недостаточно: на запись нужен живой личный токен, а он появляется
    только после того, как человек сам открыл приложение внутри портала.
    """
    async with pool().acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT m.b24_user_id
              FROM tenant_members m
              JOIN users u ON u.id = m.user_id
             WHERE m.tenant_id = $1 AND u.tg_user_id = $2
               AND m.link_status = 'authorized' AND m.b24_user_id IS NOT NULL
            """, tenant_id, tg_user_id)
    return int(value) if value is not None else None


async def tenant_of_user(tg_user_id: int) -> int | None:
    """Теннант, от имени которого действует человек.

    Нужен там, где чат ещё ничей: привязать чат может только авторизованный участник
    теннанта, и именно его теннант чат и получает. Если человек состоит в нескольких
    теннантах, автоматически выбирать нельзя — вернём None и попросим уточнить.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.tenant_id
              FROM tenant_members m
              JOIN users u ON u.id = m.user_id
              JOIN tenants t ON t.id = m.tenant_id AND t.status = 'active'
             WHERE u.tg_user_id = $1 AND m.link_status = 'authorized'
            """, tg_user_id)
    return int(rows[0]["tenant_id"]) if len(rows) == 1 else None
