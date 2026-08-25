"""Фоновый обработчик: события Битрикса, отправка уведомлений и напоминания.

Отдельный процесс, а не задача внутри бота: поллер обязан быстро крутить getUpdates,
а обработка события ходит в портал и может занять секунды. Смешивать их — значит
задерживать приём сообщений из-за чужой синхронизации.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from b24bot.core import heartbeat
from b24bot.core.config import get_settings
from b24bot.core.logging import setup as log_setup
from b24bot.crypto import box
from b24bot.db.pool import close_pool, init_pool, pool
from b24bot.domain import events, reminders, sync
from b24bot.tg import api as tg

log = logging.getLogger(__name__)

IDLE = 3.0
SEND_BATCH = 10
MAX_ATTEMPTS = 5
CHAT_LIMIT_PER_MIN = 15   # потолок Telegram — 20 сообщений в минуту на группу
RETENTION = timedelta(days=14)
# Как часто заглядывать, не пора ли обновить стадии. Сам справочник живёт сутки
# (sync.STAGE_TTL); проход обычно упирается в один запрос к базе и ничего не делает.
STAGE_PASS = timedelta(minutes=15)


async def process_events() -> int:
    rows = await events.take_pending()
    for row in rows:
        try:
            await events.process_one(row)
        except Exception:
            log.exception("событие %s не обработано", row["id"])
            async with pool().acquire() as conn:
                await conn.execute(
                    "UPDATE b24_event_inbox SET state = 'failed', "
                    "attempts = attempts + 1, processed_at = now() WHERE id = $1",
                    row["id"])
    return len(rows)


async def send_outbox() -> int:
    """Отправка с оглядкой на лимит Telegram: не больше N сообщений в минуту на чат."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE outbox SET state = 'sending' WHERE id IN (
              SELECT o.id FROM outbox o
               WHERE o.state = 'pending' AND o.next_attempt_at <= now()
                 AND (SELECT count(*) FROM outbox s
                       WHERE s.chat_ref = o.chat_ref AND s.state = 'sent'
                         AND s.sent_at > now() - interval '1 minute') < $2
               ORDER BY o.created_at LIMIT $1 FOR UPDATE SKIP LOCKED
            ) RETURNING id, tenant_id, bot_ref, chat_ref, thread_id, text, markup,
                        attempts
            """, SEND_BATCH, CHAT_LIMIT_PER_MIN)

    for row in rows:
        async with pool().acquire() as conn:
            bot = await conn.fetchrow(
                "SELECT b.bot_id, b.tenant_id, b.token, c.chat_id "
                "FROM tg_bots b JOIN tg_chats c ON c.id = $2 WHERE b.id = $1",
                row["bot_ref"], row["chat_ref"])
        if bot is None:
            await _fail(row["id"], "бот или чат исчезли", final=True)
            continue

        token = box.decrypt(bot["token"],
                            box.aad("tg_bots", "token", bot["tenant_id"], bot["bot_id"]))
        try:
            await tg.send_message(token, int(bot["chat_id"]), row["text"],
                                  thread_id=row["thread_id"],
                                  reply_markup=_markup(row))
        except tg.TelegramError as exc:
            # 403 — бота выкинули из чата, повторять бессмысленно.
            final = exc.code in (400, 403) or row["attempts"] + 1 >= MAX_ATTEMPTS
            await _fail(row["id"], f"{exc.code}: {exc.description}"[:400], final=final)
            continue

        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE outbox SET state='sent', sent_at=now() WHERE id=$1", row["id"])
    return len(rows)


def _markup(row: Any) -> dict[str, Any] | None:
    """Клавиатура из очереди. asyncpg отдаёт JSONB строкой, если нет кодека.

    Сломанная разметка не имеет права задержать сообщение: текст уведомления —
    главное, кнопки — удобство. Поэтому здесь глушим разбор, а не падаем.
    """
    raw = row["markup"]
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("клавиатура сообщения %s не разобралась, шлю без кнопок", row["id"])
        return None
    return parsed if isinstance(parsed, dict) else None


async def _fail(outbox_id: int, error: str, *, final: bool) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE outbox SET state = $2, attempts = attempts + 1, last_error = $3, "
            "next_attempt_at = now() + (interval '30 seconds' * (attempts + 1)) "
            "WHERE id = $1", outbox_id, "failed" if final else "pending", error)
    if final:
        log.warning("сообщение %s не доставлено: %s", outbox_id, error)


async def cleanup() -> None:
    """Ретенция. Журналы событий и очередь не должны расти вечно."""
    cutoff = datetime.now(UTC) - RETENTION
    async with pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM b24_event_inbox WHERE state IN ('done','dropped') "
            "AND processed_at < $1", cutoff)
        await conn.execute(
            "DELETE FROM outbox WHERE state IN ('sent','cancelled') AND sent_at < $1",
            cutoff)
        await conn.execute("DELETE FROM b24_echo_suppress WHERE expires_at < now()")
        await conn.execute(
            "DELETE FROM task_cache WHERE is_ours = false AND expires_at < now()")
        await conn.execute("DELETE FROM callback_tokens WHERE expires_at < now()")
    await reminders.cleanup_marks()


async def main() -> None:
    s = get_settings()
    log_setup(s.log_level)
    await init_pool()
    log.info("воркер запущен")

    tick = 0
    next_stage_pass = datetime.now(UTC)
    try:
        while True:
            tick += 1
            try:
                done = await process_events()
                sent = await send_outbox()
                heartbeat.beat("worker")
                # Проактивные сообщения: напоминания, эскалации, утренняя сводка.
                # Своё расписание у каждого прохода внутри, снаружи — один вызов.
                await reminders.run_due()
                if datetime.now(UTC) >= next_stage_pass:
                    # Отметка сдвигается ДО прохода: отказавший портал не должен
                    # превращать суточную синхронизацию в непрерывную.
                    next_stage_pass = datetime.now(UTC) + STAGE_PASS
                    await sync.sync_stages_due()
                if tick % 100 == 0:
                    await cleanup()
                if not done and not sent:
                    await asyncio.sleep(IDLE)
            except Exception:
                log.exception("цикл воркера упал, продолжаю")
                await asyncio.sleep(IDLE)
    finally:
        await close_pool()


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
