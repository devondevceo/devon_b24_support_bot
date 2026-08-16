"""Создание задачи из сообщения Telegram.

Заголовок и описание извлекаются БЕЗ LLM (решение заказчика: в фазе 1 модели нет).
Правила — docs/30-bot-spec.md §1.2.

Идемпотентность (И-10) построена на теге: перед созданием пишем `idem_key` в TAGS,
при повторе сначала ищем задачу по `filter[TAG]`. Проверено на портале: тег
принимается на запись и находится фильтром, хотя в getFields его нет.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from b24bot.b24 import errors
from b24bot.b24.client import B24Client
from b24bot.core.text import esc_bbcode
from b24bot.db.pool import pool
from b24bot.domain.context import ProjectRef

log = logging.getLogger(__name__)

TITLE_MAX = 80
TITLE_MIN = 10
FALLBACK_TITLE = "Обращение из Telegram"


@dataclass
class Draft:
    title: str
    description: str
    idem_key: str
    source_message_id: int | None
    # Поля полной формы мини-аппа. В чате их не спрашивают — там задача создаётся
    # одним движением, и всё, кроме заголовка, берётся по умолчанию.
    responsible_id: int | None = None
    deadline: str | None = None
    priority: int | None = None
    stage_id: int | None = None
    accomplices: list[int] | None = None
    auditors: list[int] | None = None


def extract(text: str, *, author: str, chat_title: str,
            message_link: str | None, idem_key: str,
            source_message_id: int | None = None) -> Draft:
    """Заголовок — первая строка, описание — весь текст плюс блок источника."""
    clean = "\n".join(line.rstrip() for line in (text or "").strip().splitlines())

    first = next((ln.strip() for ln in clean.splitlines() if ln.strip()), "")
    if len(first) > TITLE_MAX:
        cut = first[:TITLE_MAX]
        space = cut.rfind(" ")
        first = (cut[:space] if space > TITLE_MAX // 2 else cut) + "…"
    title = first if len(first) >= TITLE_MIN else FALLBACK_TITLE

    # Все подстановки — через esc_bbcode (И-6). Квадратные скобки Битрикс съедает
    # как разметку, а имя пользователя и название чата задаёт кто угодно.
    parts = [clean] if clean else []
    meta = ["[b]— Источник —[/b]",
            f"Telegram: чат «{esc_bbcode(chat_title)}»",
            f"Автор: {esc_bbcode(author)}"]
    if message_link:
        meta.append(f"Сообщение: {message_link}")
    parts.append("\n".join(meta))

    return Draft(title=esc_bbcode(title), description="\n\n".join(parts),
                 idem_key=idem_key, source_message_id=source_message_id)


def message_link(chat_id: int, message_id: int) -> str | None:
    """Ссылка вида t.me/c/<id>/<msg> работает только в супергруппах."""
    if chat_id >= 0:
        return None
    internal = str(chat_id)[4:] if str(chat_id).startswith("-100") else None
    return f"https://t.me/c/{internal}/{message_id}" if internal else None


async def find_existing(client: B24Client, idem_key: str) -> dict[str, Any] | None:
    """Поиск ранее созданной задачи по ключу идемпотентности."""
    try:
        res = await client.call("tasks.task.list", {
            "filter": {"TAG": idem_key},
            "select": ["ID", "TITLE", "GROUP_ID"],
        })
    except errors.B24Error:
        return None
    tasks = res.get("tasks", []) if isinstance(res, dict) else []
    return tasks[0] if tasks else None


async def create(client: B24Client, tenant_id: int, project: ProjectRef, draft: Draft,
                 responsible_id: int) -> tuple[dict[str, Any], bool]:
    """Создать задачу. Возвращает (задача, была_ли_создана_сейчас).

    Порядок обязателен: сначала запись намерения, потом поиск уже созданного,
    потом создание. Иначе ретрай после таймаута плодит дубли.
    """
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO entity_external_refs (tenant_id, idem_key, source_kind, "
            "source_key, target_kind, state) VALUES ($1,$2,'tg_message',$3,'b24_task','pending') "
            "ON CONFLICT (tenant_id, idem_key) DO NOTHING",
            tenant_id, draft.idem_key, str(draft.source_message_id or ""))

    existing = await find_existing(client, draft.idem_key)
    if existing:
        log.info("задача по ключу %s уже существует: #%s", draft.idem_key, existing.get("id"))
        return existing, False

    fields: dict[str, Any] = {
        "TITLE": draft.title,
        "DESCRIPTION": draft.description,
        "DESCRIPTION_IN_BBCODE": "Y",
        "RESPONSIBLE_ID": draft.responsible_id or responsible_id,
        "GROUP_ID": project.b24_group_id,
        "TAGS": [draft.idem_key],
    }
    # Всё это портал принимает прямо при создании — проверено записью (§9.1).
    # Пустые значения не шлём вовсе: пустой DEADLINE трактуется как «снять срок».
    if draft.deadline:
        fields["DEADLINE"] = draft.deadline
    if draft.priority is not None:
        fields["PRIORITY"] = draft.priority
    if draft.stage_id:
        fields["STAGE_ID"] = draft.stage_id
    if draft.accomplices:
        fields["ACCOMPLICES"] = draft.accomplices
    if draft.auditors:
        fields["AUDITORS"] = draft.auditors

    created = await client.call("tasks.task.add", {"fields": fields})
    task = created["task"] if isinstance(created, dict) and "task" in created else created

    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE entity_external_refs SET state='committed', target_id=$3 "
            "WHERE tenant_id=$1 AND idem_key=$2", tenant_id, draft.idem_key,
            int(task["id"]))
        await conn.execute(
            """
            INSERT INTO task_cache (tenant_id, b24_task_id, project_id, b24_group_id,
                                    is_ours, title, status, responsible_id, created_by)
            VALUES ($1,$2,$3,$4,true,$5,$6,$7,$8)
            ON CONFLICT (tenant_id, b24_task_id) DO UPDATE
              SET project_id = EXCLUDED.project_id, title = EXCLUDED.title,
                  status = EXCLUDED.status, synced_at = now()
            """,
            tenant_id, int(task["id"]), project.id, project.b24_group_id,
            task.get("title"), int(task.get("status") or 2),
            int(task.get("responsibleId") or responsible_id),
            int(task.get("createdBy") or responsible_id))

    return task, True
