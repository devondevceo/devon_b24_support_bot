"""Контекст чата: кто это, к чему привязан, что ему можно.

Инвариант И-3 живёт здесь: `authorize_task_for_chat` — единственная дверь к задаче.
Все входы (/t_<id>, /comment, ввод номера, ввод URL, любой callback) обязаны
проходить через неё, и отказ у неё всегда один и тот же — иначе перебор номеров
работает как оракул существования.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from b24bot.db.pool import pool

log = logging.getLogger(__name__)

TASK_NOT_FOUND = "задача не найдена в проектах этого чата"


@dataclass
class ProjectRef:
    id: int
    b24_group_id: int
    name: str
    client_name: str


@dataclass
class ChatContext:
    chat_ref: int
    chat_id: int
    title: str
    status: str
    tenant_id: int | None
    is_forum: bool
    thread_id: int | None = None
    projects: list[ProjectRef] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.tenant_id is not None and self.status in ("claimed", "active")

    @property
    def has_binding(self) -> bool:
        return bool(self.projects)


async def load_chat_context(chat_id: int, thread_id: int | None = None) -> ChatContext | None:
    """Всё, что нужно знать о чате, одним запросом.

    Привязка на топик имеет приоритет над привязкой на весь чат: если для этого
    топика есть свои проекты, берутся они.
    """
    async with pool().acquire() as conn:
        chat = await conn.fetchrow(
            "SELECT id, chat_id, title, status, tenant_id, is_forum "
            "FROM tg_chats WHERE chat_id = $1 AND status <> 'migrated'", chat_id)
        if chat is None:
            return None

        rows = await conn.fetch(
            """
            SELECT p.id, p.b24_group_id, p.name, c.name AS client_name,
                   b.topic_ref, t.thread_id
              FROM chat_bindings b
              JOIN projects p ON p.id = b.project_id AND p.status = 'active'
              JOIN clients  c ON c.id = p.client_id
              LEFT JOIN tg_topics t ON t.id = b.topic_ref
             WHERE b.chat_ref = $1 AND b.status = 'active'
            """, chat["id"])

    scoped = [r for r in rows if thread_id is not None and r["thread_id"] == thread_id]
    chosen = scoped or [r for r in rows if r["topic_ref"] is None]

    return ChatContext(
        chat_ref=chat["id"], chat_id=chat["chat_id"], title=chat["title"] or "",
        status=chat["status"], tenant_id=chat["tenant_id"],
        is_forum=chat["is_forum"], thread_id=thread_id,
        projects=[ProjectRef(r["id"], r["b24_group_id"], r["name"], r["client_name"])
                  for r in chosen],
    )


async def project_by_group(tenant_id: int, chat_ref: int,
                           b24_group_id: int) -> ProjectRef | None:
    """Проект чата по группе Битрикса."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.id, p.b24_group_id, p.name, c.name AS client_name
              FROM projects p
              JOIN clients c ON c.id = p.client_id
              JOIN chat_bindings b ON b.project_id = p.id AND b.status = 'active'
             WHERE p.tenant_id = $1 AND p.b24_group_id = $2 AND p.status = 'active'
               AND b.chat_ref = $3
             LIMIT 1
            """, tenant_id, b24_group_id, chat_ref)
    if row is None:
        return None
    return ProjectRef(row["id"], row["b24_group_id"], row["name"], row["client_name"])


async def authorize_task_for_chat(tenant_id: int, chat_ref: int, b24_task_id: int, *,
                                  group_id_hint: int | None = None) -> ProjectRef | None:
    """Инвариант И-3. Задача обязана принадлежать проекту, привязанному к ЭТОМУ чату.

    Сначала смотрим кэш, но его отсутствие НЕ означает отказ: кэш наполняется
    событиями и созданием задач, поэтому давние задачи портала в нём отсутствуют.
    Список же берётся живьём — из-за этого карточка отвечала «задача не найдена»
    на задачу, которую сама же и показала. Поэтому вызывающий может передать
    группу задачи, узнанную дозапросом, и проверка идёт по ней.

    Возвращает проект или None. На None вызывающий обязан отвечать одним и тем же
    текстом, не раскрывая, существует ли задача вообще.
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT p.id, p.b24_group_id, p.name, c.name AS client_name
              FROM task_cache tc
              JOIN projects p ON p.id = tc.project_id AND p.status = 'active'
              JOIN clients  c ON c.id = p.client_id
              JOIN chat_bindings b ON b.project_id = p.id AND b.status = 'active'
             WHERE tc.tenant_id = $1 AND tc.b24_task_id = $2 AND tc.is_ours
               AND b.chat_ref = $3
             LIMIT 1
            """, tenant_id, b24_task_id, chat_ref)
    if row is not None:
        return ProjectRef(row["id"], row["b24_group_id"], row["name"], row["client_name"])

    if group_id_hint is not None:
        found = await project_by_group(tenant_id, chat_ref, group_id_hint)
        if found is not None:
            return found

    log.info("отказ в доступе к задаче %s из чата %s", b24_task_id, chat_ref)
    return None


async def remember_task(tenant_id: int, project: ProjectRef, task: dict[str, Any]) -> None:
    """Подгрузить задачу в кэш после успешной проверки доступа."""
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_cache (tenant_id, b24_task_id, project_id, b24_group_id,
                is_ours, title, status, stage_id, responsible_id, created_by, synced_at)
            VALUES ($1,$2,$3,$4,true,$5,$6,$7,$8,$9,now())
            ON CONFLICT (tenant_id, b24_task_id) DO UPDATE
              SET project_id = EXCLUDED.project_id, is_ours = true,
                  title = EXCLUDED.title, status = EXCLUDED.status,
                  stage_id = EXCLUDED.stage_id, synced_at = now()
            """,
            tenant_id, int(task["id"]), project.id, project.b24_group_id,
            task.get("title"), _int(task.get("status")), _int(task.get("stageId")),
            _int(task.get("responsibleId")), _int(task.get("createdBy")))


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def allowed_group_ids(tenant_id: int, chat_ref: int) -> list[int]:
    """Список групп Битрикса, разрешённых в этом чате.

    Обязательный аргумент всех выборок задач. Сервисной учётки с урезанной видимостью
    у нас нет (решение заказчика), поэтому область видимости задаёт только этот список —
    см. docs/40-security.md §1.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT p.b24_group_id FROM chat_bindings b "
            "JOIN projects p ON p.id = b.project_id AND p.status = 'active' "
            "WHERE b.tenant_id = $1 AND b.chat_ref = $2 AND b.status = 'active'",
            tenant_id, chat_ref)
    return [int(r["b24_group_id"]) for r in rows]


# ------------------------------------------------------------------- кнопки
def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def issue_token(kind: str, *, tenant_id: int | None = None,
                      owner_tg_id: int | None = None, chat_ref: int | None = None,
                      payload: dict[str, Any] | None = None,
                      ttl: timedelta = timedelta(minutes=30),
                      single_use: bool = True) -> str:
    import json

    token = secrets.token_urlsafe(16)
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO callback_tokens (token_hash, tenant_id, kind, owner_tg_id, "
            "chat_ref, payload, single_use, expires_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            _hash(token), tenant_id, kind, owner_tg_id, chat_ref,
            json.dumps(payload or {}, ensure_ascii=False), single_use,
            datetime.now(UTC) + ttl)
    return token


async def consume_token(token: str, actor_tg_id: int | None) -> Any:
    """Пять проверок на каждое нажатие, все каждый раз заново (docs/30-bot-spec.md §0.2)."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT token_hash, tenant_id, kind, owner_tg_id, chat_ref, payload, "
            "single_use, used_at FROM callback_tokens "
            "WHERE token_hash = $1 AND expires_at > now()", _hash(token))
        if row is None:
            return None
        if row["single_use"] and row["used_at"] is not None:
            return None
        if row["owner_tg_id"] is not None and row["owner_tg_id"] != actor_tg_id:
            log.warning("чужое нажатие кнопки: владелец %s, нажал %s",
                        row["owner_tg_id"], actor_tg_id)
            return None
        if row["single_use"]:
            await conn.execute(
                "UPDATE callback_tokens SET used_at = now() WHERE token_hash = $1",
                row["token_hash"])
    return row
