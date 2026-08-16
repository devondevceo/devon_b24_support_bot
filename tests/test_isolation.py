"""Изоляция теннантов и чатов: инварианты И-2 и И-3 на живой базе.

Единственные тесты, которым нужна настоящая PostgreSQL: проверять SQL по памяти
бессмысленно, ошибка изоляции живёт именно в тексте запроса. Поднимают отдельную
базу, накатывают все миграции, создают двух теннантов с одинаковой формой данных
и убеждаются, что ни один запрос не видит чужого.

Запуск: `TEST_DATABASE_URL=postgresql://user:pass@host:5432/postgres pytest`.
Без переменной модуль пропускается — на ноутбуке без базы сборка не должна краснеть.

Повод: в `authorize_task_for_chat` добавлена подсказка `group_id_hint` — карточка
задачи стала проверять принадлежность по фактической группе, а не только по кэшу.
Это расширило дверь к задаче, и цена ошибки здесь — чужая переписка в чужом чате.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def db_url() -> str:
    """Отдельная база на модуль: миграции накатываются с нуля, мусор не переживает прогон."""
    import asyncio

    import asyncpg

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


async def _fixture_world(conn: object) -> dict[str, int]:
    """Два теннанта одинаковой формы. Второй существует затем, чтобы его было видно,
    если запрос забыл `tenant_id`.

    Уникальный префикс на вызов: база одна на модуль, а `b24_member_id` и `chat_id`
    уникальны глобально — иначе второй тест падал бы на чужих данных, а не на сути.
    """
    uniq = uuid.uuid4().hex[:8]
    base = -abs(hash(uniq)) % 10_000_000 * 100
    ids: dict[str, int] = {}
    for n, tag in ((1, "a"), (2, "b")):
        t = await conn.fetchval(  # type: ignore[attr-defined]
            "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
            "VALUES ($1, $2, $3, $4, 'active') RETURNING id",
            f"t{uniq}{tag}", f"Теннант {tag}", f"member-{uniq}-{tag}",
            f"t{uniq}{n}.bitrix24.ru")
        c = await conn.fetchval(  # type: ignore[attr-defined]
            "INSERT INTO clients (tenant_id, name, status) "
            "VALUES ($1, $2, 'active') RETURNING id", t, f"Клиент {tag}")
        # Одинаковый b24_group_id у обоих теннантов — это законно: порталы разные.
        p = await conn.fetchval(  # type: ignore[attr-defined]
            "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
            "VALUES ($1, $2, 33, $3, 'active') RETURNING id", t, c, f"Проект {tag}")
        ch = await conn.fetchval(  # type: ignore[attr-defined]
            "INSERT INTO tg_chats (tenant_id, chat_id, title, type, status) "
            "VALUES ($1, $2, $3, 'group', 'active') RETURNING id",
            t, -(base + n), f"Чат {tag}")
        await conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id, status) "
            "VALUES ($1, $2, $3, 'active')", t, ch, p)
        ids |= {f"tenant_{tag}": t, f"client_{tag}": c,
                f"project_{tag}": p, f"chat_{tag}": ch}
    return ids


async def test_hint_does_not_open_another_tenants_task(db: object) -> None:
    """Главный риск новой подсказки: у обоих теннантов группа 33.

    Если бы `project_by_group` искал только по `b24_group_id`, чат теннанта «а»
    открыл бы задачу теннанта «б» — при том, что это вообще другой портал.
    """
    from b24bot.domain.context import authorize_task_for_chat

    w = await _fixture_world(db)

    own = await authorize_task_for_chat(w["tenant_a"], w["chat_a"], 500,
                                        group_id_hint=33)
    assert own is not None and own.id == w["project_a"]

    alien = await authorize_task_for_chat(w["tenant_b"], w["chat_a"], 500,
                                          group_id_hint=33)
    assert alien is None, "чат теннанта «а» отдал задачу под теннантом «б»"


async def test_hint_does_not_open_a_task_of_another_chat(db: object) -> None:
    """И-3 в чистом виде: группа существует у теннанта, но не привязана к ЭТОМУ чату."""
    from b24bot.domain.context import authorize_task_for_chat

    w = await _fixture_world(db)
    other_project = await db.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
        "VALUES ($1, $2, 77, 'Чужой проект', 'active') RETURNING id",
        w["tenant_a"], w["client_a"])
    other_chat = await db.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO tg_chats (tenant_id, chat_id, title, type, status) "
        "VALUES ($1, $2, 'Чужой чат', 'group', 'active') RETURNING id",
        w["tenant_a"], -(w["chat_a"] * 1_000 + 9001))
    await db.execute(  # type: ignore[attr-defined]
        "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id, status) "
        "VALUES ($1, $2, $3, 'active')", w["tenant_a"], other_chat, other_project)

    assert await authorize_task_for_chat(w["tenant_a"], w["chat_a"], 600,
                                         group_id_hint=77) is None


async def test_number_scan_finds_nothing(db: object) -> None:
    """Перебор номеров задач из чужого чата обязан молчать на всём диапазоне.

    Кэш наполнен задачами теннанта «б», подсказка не передаётся — ровно то, что
    сделает злоумышленник, подставляя `/t_<id>` в своём чате.
    """
    from b24bot.domain.context import authorize_task_for_chat

    w = await _fixture_world(db)
    for task_id in range(100, 120):
        await db.execute(  # type: ignore[attr-defined]
            "INSERT INTO task_cache (tenant_id, b24_task_id, project_id, "
            "b24_group_id, is_ours, title) VALUES ($1,$2,$3,33,true,$4)",
            w["tenant_b"], task_id, w["project_b"], f"Секрет {task_id}")

    for task_id in range(100, 120):
        assert await authorize_task_for_chat(w["tenant_a"], w["chat_a"], task_id) is None


async def test_disabled_binding_closes_the_door(db: object) -> None:
    """Снятая привязка обязана закрывать и подсказку тоже, иначе отзыв доступа фиктивен."""
    from b24bot.domain.context import authorize_task_for_chat

    w = await _fixture_world(db)
    await db.execute(  # type: ignore[attr-defined]
        "UPDATE chat_bindings SET status = 'disabled' WHERE chat_ref = $1", w["chat_a"])

    assert await authorize_task_for_chat(w["tenant_a"], w["chat_a"], 500,
                                         group_id_hint=33) is None


async def test_every_domain_table_carries_tenant_id(db: object) -> None:
    """И-2: страж списка таблиц.

    Таблица без `tenant_id` — это таблица, в которой изоляция держится на честном
    слове вызывающего. Новая таблица либо получает колонку, либо явно вносится
    в список исключений вместе с причиной.
    """
    exempt = {
        "alembic_version",   # служебная таблица Alembic
        "tenants",           # сам теннант; его ключ — id
        "enc_keys",          # ключи шифрования общие на инсталляцию
        "users",             # глобальная запись человека: один Telegram-аккаунт
                             # может состоять сразу в нескольких теннантах,
                             # привязка живёт в tenant_members
        "b24_payload_log",   # журнал СТРУКТУРЫ входящих payload: пишется до того,
                             # как теннант вообще определён (событие install).
                             # Таблица спайковая и удаляется отдельной миграцией,
                             # см. docs/80-deploy.md
    }
    rows = await db.fetch(  # type: ignore[attr-defined]
        """
        SELECT t.table_name,
               bool_or(c.column_name = 'tenant_id') AS has_tenant
          FROM information_schema.tables t
          JOIN information_schema.columns c
            ON c.table_schema = t.table_schema AND c.table_name = t.table_name
         WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
         GROUP BY t.table_name
        """)
    missing = sorted(r["table_name"] for r in rows
                     if not r["has_tenant"] and r["table_name"] not in exempt)
    assert not missing, f"таблицы без tenant_id: {missing}"
