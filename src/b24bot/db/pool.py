"""Пул соединений PostgreSQL.

Размер пула считается как workers x max_size — процесс api запускается одним воркером
(см. Dockerfile), поэтому 8 соединений это и есть 8, а не 8 x N.
"""
from __future__ import annotations

import asyncpg

from b24bot.core.config import get_settings

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str | None = None) -> asyncpg.Pool:
    """`dsn` задают только тесты изоляции: настройки кэшируются, подменять их поздно."""
    global _pool
    if _pool is None:
        s = get_settings()
        _pool = await asyncpg.create_pool(
            dsn=dsn or s.asyncpg_dsn,
            min_size=1,
            max_size=8,
            command_timeout=30,
            max_inactive_connection_lifetime=300,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("пул не инициализирован")
    return _pool
