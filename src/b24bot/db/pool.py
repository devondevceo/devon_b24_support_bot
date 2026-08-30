"""Пул соединений PostgreSQL и контекст теннанта для RLS.

Размер пула считается как workers x max_size — процесс api запускается одним воркером
(см. Dockerfile), поэтому 8 соединений это и есть 8, а не 8 x N.

## Второй рубеж изоляции (RLS, миграция 0020)

Первый рубеж — обязательный `tenant_id` в каждом запросе (И-2). Второй — политики
Row-Level Security в самой базе: даже запрос с забытым фильтром или пролезшая
инъекция не увидят чужих строк. Политика читает две GUC-переменные:

* `app.rls`   — `'enforce'` включает политику для ЭТОГО соединения. Ставится здесь
  же при захвате соединения, когда `RLS_ENFORCE=true`. Соединение, не объявившее
  enforce (старый образ при новой схеме, psql руками, alembic), работает как
  раньше — это и есть вперёд-совместимость миграции.
* `app.ctx`   — `'<tenant_id>'`, `'system'` или пусто. Пусто под enforce означает
  «не видно ничего»: путь, забывший объявить контекст, ломается громко, а не
  читает чужое молча.

Контекст живёт в `contextvars` и объявляется тремя способами:

* `tenant_scope(tenant_id)` — блок работает от имени теннанта; это правило по
  умолчанию для всего, что обслуживает конкретного человека или портал.
* `system_scope()` — блок видит все теннанты. Только для инфраструктуры, которая
  кросс-теннантна ПО ПОСТРОЕНИЮ: выборки воркера из общих очередей, реестр
  поллеров, резолв теннанта по недоверенному идентификатору (member_id,
  webhook_id, токен сессии) — питоновский аналог SECURITY DEFINER-резолверов
  из docs/20-data-model.md §13. Скоуп сужается до первого же резолва.
* `set_tenant(tenant_id)` — то же, что `tenant_scope`, но без блока: для мест,
  где контекст должен пережить возврат из функции (`load_session` объявляет
  теннанта на весь остаток HTTP-запроса — у ASGI каждый запрос живёт в своей
  задаче, и contextvar умирает вместе с ней).

Каждый захват соединения (при включённом enforce) выставляет обе переменные одним
запросом — на отпущенном соединении контекст не живёт, чужому захватчику он не
достанется. При `RLS_ENFORCE=false` фасад не делает ничего: ни одного лишнего
запроса на боевом пути, пока включение не решено явно (docs/80-deploy.md §9).
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from types import TracebackType

import asyncpg

from b24bot.core.config import get_settings

_pool: asyncpg.Pool | None = None

# '' — контекст не объявлен (под enforce не видно ничего), 'system' — все
# теннанты, иначе — id теннанта строкой (GUC умеют только текст).
_ctx: ContextVar[str] = ContextVar("b24bot_db_ctx", default="")


def current_ctx() -> str:
    return _ctx.get()


def set_tenant(tenant_id: int) -> None:
    """Объявить теннанта до конца текущей задачи (см. докстринг модуля)."""
    _ctx.set(str(int(tenant_id)))


@contextlib.contextmanager
def tenant_scope(tenant_id: int) -> Iterator[None]:
    token = _ctx.set(str(int(tenant_id)))
    try:
        yield
    finally:
        _ctx.reset(token)


@contextlib.contextmanager
def system_scope() -> Iterator[None]:
    token = _ctx.set("system")
    try:
        yield
    finally:
        _ctx.reset(token)


class _Acquire:
    """`async with pool().acquire() as conn` — тот же контракт, что у asyncpg.

    Единственная добавка — объявление GUC при входе. Наружу отдаётся сырое
    соединение asyncpg: `conn.transaction()`, `fetch`, `executemany` и всё
    остальное работает как раньше.
    """

    __slots__ = ("_conn", "_pool")

    def __init__(self, raw_pool: asyncpg.Pool) -> None:
        self._pool = raw_pool
        self._conn: asyncpg.Connection | None = None

    async def __aenter__(self) -> asyncpg.Connection:
        conn = await self._pool.acquire()
        try:
            if get_settings().rls_enforce:
                await conn.execute(
                    "SELECT set_config('app.rls', 'enforce', false), "
                    "set_config('app.ctx', $1, false)", _ctx.get())
        except BaseException:
            await self._pool.release(conn)
            raise
        self._conn = conn
        return conn

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, tb: TracebackType | None) -> None:
        if self._conn is not None:
            await self._pool.release(self._conn)
            self._conn = None


class PoolFacade:
    """Единственная дверь к базе: все обращения идут через `acquire()`."""

    __slots__ = ("_raw",)

    def __init__(self, raw_pool: asyncpg.Pool) -> None:
        self._raw = raw_pool

    def acquire(self) -> _Acquire:
        return _Acquire(self._raw)

    async def close(self) -> None:
        await self._raw.close()


_facade: PoolFacade | None = None


async def init_pool(dsn: str | None = None) -> PoolFacade:
    """`dsn` задают только тесты изоляции: настройки кэшируются, подменять их поздно."""
    global _pool, _facade
    if _pool is None:
        s = get_settings()
        _pool = await asyncpg.create_pool(
            dsn=dsn or s.asyncpg_dsn,
            min_size=1,
            max_size=8,
            command_timeout=30,
            max_inactive_connection_lifetime=300,
        )
        _facade = PoolFacade(_pool)
    assert _facade is not None
    return _facade


async def close_pool() -> None:
    global _pool, _facade
    if _pool is not None:
        await _pool.close()
        _pool = None
        _facade = None


def pool() -> PoolFacade:
    if _facade is None:
        raise RuntimeError("пул не инициализирован")
    return _facade
