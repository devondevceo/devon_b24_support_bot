"""Движок сценарного опросника.

LLM в фазе 1 нет, и уточняющие вопросы задаёт сценарий. Это даже лучше: набор
вопросов предсказуем, отвечает мгновенно, ничего не выдумывает и правится в БД
без релиза.

Два правила, за которыми стоят конкретные грабли:

* **Ответ принимается ТОЛЬКО реплаем на вопрос бота.** Приём «любого следующего
  сообщения владельца сессии» в общем чате съедал бы обычные реплики коллегам
  («ага», «щас гляну») и отправлял их в описание задачи в Битриксе.
* **Сессия ключуется чатом, топиком и автором.** В одном чате несколько человек
  ведут опросники одновременно и не мешают друг другу.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from b24bot.core.text import esc_html
from b24bot.db.pool import pool

log = logging.getLogger(__name__)

TTL = timedelta(minutes=30)


@dataclass
class Question:
    code: str
    text: str
    required: bool


@dataclass
class Session:
    id: int
    tenant_id: int
    chat_ref: int
    thread_id: int
    owner_tg_id: int
    template_id: int
    project_id: int | None
    step: int
    answers: dict[str, str]
    last_message_id: int | None


def _now() -> datetime:
    return datetime.now(UTC)


async def categories(tenant_id: int) -> list[tuple[int, str]]:
    """Наборы вопросов. Свой набор теннанта перекрывает системный по коду."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (code) id, code, title
              FROM survey_templates
             WHERE is_active AND (tenant_id = $1 OR tenant_id IS NULL)
             ORDER BY code, tenant_id NULLS LAST
            """, tenant_id)
        order = await conn.fetch(
            "SELECT id, sort FROM survey_templates WHERE id = ANY($1::bigint[])",
            [r["id"] for r in rows])
    sort_by_id = {r["id"]: r["sort"] for r in order}
    items = [(r["id"], r["title"]) for r in rows]
    items.sort(key=lambda x: sort_by_id.get(x[0], 0))
    return items


async def questions(tenant_id: int, template_id: int) -> list[Question]:
    """Инвариант И-2: `tenant_id` обязателен и здесь.

    NULL в колонке — системный шаблон, общий для всех теннантов; всё остальное
    видит только свой теннант. Без этого условия вопрос чужого шаблона попадал
    в опрос по одному лишь `template_id`.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT code, text, required FROM survey_questions "
            "WHERE template_id = $1 AND (tenant_id = $2 OR tenant_id IS NULL) "
            "ORDER BY sort", template_id, tenant_id)
    return [Question(r["code"], r["text"], r["required"]) for r in rows]


async def start(tenant_id: int, chat_ref: int, thread_id: int | None,
                owner_tg_id: int, template_id: int,
                project_id: int | None) -> Session:
    """Начать опрос. Прежняя незавершённая сессия того же человека отменяется."""
    async with pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE survey_sessions SET state = 'cancelled' "
            "WHERE chat_ref = $1 AND thread_id = $2 AND owner_tg_id = $3 "
            "AND state = 'active'", chat_ref, thread_id or 0, owner_tg_id)
        row = await conn.fetchrow(
            "INSERT INTO survey_sessions (tenant_id, chat_ref, thread_id, owner_tg_id, "
            "template_id, project_id, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7) "
            "RETURNING id", tenant_id, chat_ref, thread_id or 0, owner_tg_id,
            template_id, project_id, _now() + TTL)
    return Session(int(row["id"]), tenant_id, chat_ref, thread_id or 0, owner_tg_id,
                   template_id, project_id, 0, {}, None)


async def active_for(chat_ref: int, thread_id: int | None,
                     owner_tg_id: int) -> Session | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, tenant_id, chat_ref, thread_id, owner_tg_id, template_id, "
            "project_id, step, answers, last_message_id FROM survey_sessions "
            "WHERE chat_ref = $1 AND thread_id = $2 AND owner_tg_id = $3 "
            "AND state = 'active' AND expires_at > now()",
            chat_ref, thread_id or 0, owner_tg_id)
    if row is None:
        return None
    answers = row["answers"]
    return Session(
        int(row["id"]), int(row["tenant_id"]), int(row["chat_ref"]),
        int(row["thread_id"]), int(row["owner_tg_id"]), int(row["template_id"]),
        row["project_id"], int(row["step"]),
        json.loads(answers) if isinstance(answers, str) else dict(answers or {}),
        row["last_message_id"])


async def remember_question(session_id: int, message_id: int) -> None:
    """Запомнить, на какое сообщение ждём реплай."""
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE survey_sessions SET last_message_id = $2 WHERE id = $1",
            session_id, message_id)


async def record(session: Session, code: str, answer: str) -> None:
    session.answers[code] = answer
    session.step += 1
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE survey_sessions SET step = $2, answers = $3, expires_at = $4 "
            "WHERE id = $1", session.id, session.step,
            json.dumps(session.answers, ensure_ascii=False), _now() + TTL)


async def skip(session: Session) -> None:
    session.step += 1
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE survey_sessions SET step = $2, expires_at = $3 WHERE id = $1",
            session.id, session.step, _now() + TTL)


async def finish(session_id: int, state: str = "done") -> None:
    async with pool().acquire() as conn:
        await conn.execute("UPDATE survey_sessions SET state = $2 WHERE id = $1",
                           session_id, state)


async def expire_stale() -> int:
    """Брошенные опросы закрываются, чтобы не висеть вечно и не блокировать новые."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "UPDATE survey_sessions SET state = 'expired' "
            "WHERE state = 'active' AND expires_at < now() RETURNING id")
    return len(rows)


def assemble(items: list[Question], answers: dict[str, str]) -> tuple[str, str]:
    """Собрать заголовок и описание из ответов.

    Заголовок — ответ на первый вопрос: он же и есть суть обращения. Описание —
    пары «вопрос → ответ», так исполнителю видно, что именно спрашивали.
    """
    first = next((answers.get(q.code) for q in items if answers.get(q.code)), "")
    title = first.strip().splitlines()[0] if first else "Обращение из Telegram"

    lines = []
    for q in items:
        value = answers.get(q.code)
        if value:
            lines.append(f"[b]{q.text}[/b]\n{value}")
    return title, "\n\n".join(lines)


def render_preview(title: str, items: list[Question], answers: dict[str, str],
                   project_name: str) -> str:
    rows = ["📝 <b>Черновик задачи</b>", "",
            f"<b>{esc_html(title)}</b>",
            f"Проект: {esc_html(project_name)}", ""]
    for q in items:
        value = answers.get(q.code)
        if value:
            rows.append(f"<b>{esc_html(q.text)}</b>")
            rows.append(esc_html(value))
    return "\n".join(rows)


def progress(step: int, total: int) -> str:
    return f"<i>Вопрос {step + 1} из {total}</i>"


def question_text(q: Question, step: int, total: int) -> str:
    tail = "" if q.required else "\n<i>Можно пропустить.</i>"
    return (f"{progress(step, total)}\n\n<b>{esc_html(q.text)}</b>{tail}\n\n"
            f"<i>Ответьте на это сообщение.</i>")


def answers_as_dict(session: Session) -> dict[str, Any]:
    return dict(session.answers)
