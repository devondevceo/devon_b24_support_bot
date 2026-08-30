"""Alembic: асинхронный движок на asyncpg, URL строго из окружения."""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _url() -> str:
    # После включения RLS (docs/80-deploy.md §9) DATABASE_URL приложения ходит
    # ролью b24bot_app без DDL-прав — миграциям нужен владелец. Отдельная
    # переменная разводит их; пока она не задана, поведение прежнее.
    url = os.environ.get("MIGRATIONS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL не задан — миграции запускать нечем")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


config.set_main_option("sqlalchemy.url", _url())

# Схема описывается вручную в миграциях (сырой SQL), автогенерация не используется:
# у нас есть частичные индексы, домены, RLS и партиции, которые autogenerate не умеет.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
