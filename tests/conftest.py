"""Общая настройка тестов.

Окружение выставляется ДО импорта модулей приложения: конфигурация читается один раз
и кэшируется, поэтому подменять её потом поздно.

Здесь же живут фикстуры настоящей PostgreSQL. Они нужны больше чем одному модулю
(изоляция теннантов, синхронизация стадий), а копия фикстуры в каждом файле — это
две базы на прогон и два места, где чинить пересоздание схемы.
"""
from __future__ import annotations

import base64
import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault(
    "MASTER_KEY",
    base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode().rstrip("="),
)
os.environ.setdefault("MASTER_KEY_ID", "1")
os.environ.setdefault("DOMAIN", "b24sdbot.devondev.ru")
os.environ.setdefault("B24_CLIENT_ID", "test.client")
os.environ.setdefault("B24_CLIENT_SECRET", "test.secret")

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def db_url() -> Iterator[str]:
    """Отдельная база на модуль: миграции накатываются с нуля, мусор не переживает прогон."""
    import asyncio
    import subprocess

    import asyncpg

    if not ADMIN_URL:
        pytest.skip("нужна TEST_DATABASE_URL")

    name = f"b24iso_{uuid.uuid4().hex[:12]}"

    async def create() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        await conn.execute(f'CREATE DATABASE "{name}"')
        await conn.close()

    async def drop() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()", name)
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await conn.close()

    asyncio.run(create())
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args: str) -> None:
        # Аргументы — литералы этого файла, внешнего ввода здесь нет.
        done = subprocess.run([sys.executable, "-m", "alembic", *args],  # noqa: S603
                              cwd=ROOT, env=env, capture_output=True, text=True)
        if done.returncode != 0:
            asyncio.run(drop())
            pytest.fail(f"alembic {' '.join(args)}:\n{done.stdout}\n{done.stderr}")

    # Обратная совместимость обязательна (CLAUDE.md): вся цепочка обязана
    # разворачиваться и накатываться заново. Проверяем на каждом прогоне —
    # сломанный downgrade обнаружится в тот же день, а не в день отката.
    alembic("upgrade", "head")
    alembic("downgrade", "base")
    alembic("upgrade", "head")
    try:
        yield url
    finally:
        asyncio.run(drop())


@pytest.fixture
async def db(db_url: str) -> AsyncIterator[object]:
    """Пул приложения на тестовую базу — код ходит ровно теми же запросами."""
    from b24bot.db import pool as pool_mod

    await pool_mod.init_pool(db_url)
    try:
        async with pool_mod.pool().acquire() as conn:
            yield conn
    finally:
        await pool_mod.close_pool()
