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
from b24bot.domain import events, lifecycle, notifications, reminders, sync
from b24bot.tg import api as tg

log = logging.getLogger(__name__)

IDLE = 3.0
SEND_BATCH = 10
MAX_ATTEMPTS = 5
CHAT_LIMIT_PER_MIN = 15   # потолок Telegram — 20 сообщений в минуту на группу
RETENTION = timedelta(days=14)
# Требование Маркета: журнал вызовов API за ПОСЛЕДНИЕ 3 суток. Держим ровно их.
CALL_LOG_RETENTION = timedelta(days=3)
# Как часто заглядывать, не пора ли обновить стадии. Сам справочник живёт сутки
# (sync.STAGE_TTL); проход обычно упирается в один запрос к базе и ничего не делает.
STAGE_PASS = timedelta(minutes=15)
# Жизненный цикл теннантов: подписка Маркета, подписки на события, чистка
# деинсталлированных. Сам проход решает, кому пора (lifecycle.LICENSE_TTL);
# час — это частота, с которой мы об этом спрашиваем базу.
LIFECYCLE_PASS = timedelta(hours=1)


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
    """Отправка с оглядкой на лимит Telegram: не больше N сообщений в минуту на чат.

    Накопительные строки сюда не попадают: у них своё время отправки и своя
    сборка в одно сообщение (`flush_digests`).
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE outbox SET state = 'sending' WHERE id IN (
              SELECT o.id FROM outbox o
               WHERE o.state = 'pending' AND NOT o.digest AND o.next_attempt_at <= now()
                 AND (SELECT count(*) FROM outbox s
                       WHERE s.chat_ref = o.chat_ref AND s.state = 'sent'
                         AND s.sent_at > now() - interval '1 minute') < $2
               ORDER BY o.created_at LIMIT $1 FOR UPDATE SKIP LOCKED
            ) RETURNING id, tenant_id, bot_ref, chat_ref, thread_id, text, markup,
                        attempts
            """, SEND_BATCH, CHAT_LIMIT_PER_MIN)

    for row in rows:
        creds = await _creds(row)
        if creds is None:
            await _fail(row["id"], "бот или чат исчезли", final=True)
            continue
        token, chat_id = creds
        try:
            await tg.send_message(token, chat_id, row["text"],
                                  thread_id=row["thread_id"],
                                  reply_markup=_markup(row))
        except tg.TelegramError as exc:
            # 403 — бота выкинули из чата, повторять бессмысленно.
            final = exc.code in (400, 403) or row["attempts"] + 1 >= MAX_ATTEMPTS
            await _fail(row["id"], f"{exc.code}: {exc.description}"[:400], final=final)
            continue

        await _sent([row["id"]])
    return len(rows)


async def flush_digests() -> int:
    """Отправить сводки, чьё окно закрылось: один чат — одно сообщение.

    Три решения, которые видно в коде:

    1. **Окно с одной новостью — это не сводка.** Такая строка уходит обычным
       уведомлением, со своими кнопками: заворачивать одну новость в шапку
       «сводка · 1 уведомление» значило бы отнять у неё действия ради формы.
    2. **У сводки кнопок нет.** Двадцать новостей — это двадцать наборов кнопок,
       и ни один из них не относится к сообщению целиком. Номер задачи в каждой
       строке остаётся ссылкой на портал, а карточка открывается командой
       `/t_<номер>`, как и раньше.
    3. **Не влезло в одно сообщение — уходит следующим**, а не обрезается.
    """
    async with pool().acquire() as conn:
        groups = await conn.fetch(
            """
            SELECT o.tenant_id, o.bot_ref, o.chat_ref, o.thread_id
              FROM outbox o
             WHERE o.state = 'pending' AND o.digest AND o.next_attempt_at <= now()
               AND (SELECT count(*) FROM outbox s
                     WHERE s.chat_ref = o.chat_ref AND s.state = 'sent'
                       AND s.sent_at > now() - interval '1 minute') < $1
             GROUP BY 1, 2, 3, 4 LIMIT $2
            """, CHAT_LIMIT_PER_MIN, SEND_BATCH)

    done = 0
    for g in groups:
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                UPDATE outbox SET state = 'sending' WHERE id IN (
                  SELECT id FROM outbox
                   WHERE tenant_id = $1 AND chat_ref = $2
                     AND thread_id IS NOT DISTINCT FROM $3
                     AND state = 'pending' AND digest AND next_attempt_at <= now()
                   ORDER BY created_at FOR UPDATE SKIP LOCKED
                ) RETURNING id, text, digest_text, markup, attempts, created_at
                """, g["tenant_id"], g["chat_ref"], g["thread_id"])
        if not rows:
            continue
        done += len(rows)

        creds = await _creds(g)
        if creds is None:
            for row in rows:
                await _fail(row["id"], "бот или чат исчезли", final=True)
            continue
        token, chat_id = creds

        if len(rows) == 1:
            texts: list[str] = [rows[0]["text"]]
            markup = _markup(rows[0])
        else:
            span = datetime.now(UTC) - min(r["created_at"] for r in rows)
            texts = notifications.digest_messages(
                [r["digest_text"] or r["text"].replace("\n", " · ") for r in rows], span)
            markup = None

        try:
            for text in texts:
                await tg.send_message(token, chat_id, text,
                                      thread_id=g["thread_id"], reply_markup=markup)
        except tg.TelegramError as exc:
            final = exc.code in (400, 403) or rows[0]["attempts"] + 1 >= MAX_ATTEMPTS
            for row in rows:
                await _fail(row["id"], f"{exc.code}: {exc.description}"[:400],
                            final=final)
            continue

        await _sent([r["id"] for r in rows])
        log.info("сводка из %d уведомлений отправлена в чат %s (%d сообщени(й))",
                 len(rows), g["chat_ref"], len(texts))
    return done


async def _creds(row: Any) -> tuple[str, int] | None:
    """Токен бота и chat_id Telegram для строки очереди."""
    async with pool().acquire() as conn:
        bot = await conn.fetchrow(
            "SELECT b.bot_id, b.tenant_id, b.token, c.chat_id "
            "FROM tg_bots b JOIN tg_chats c ON c.id = $2 WHERE b.id = $1",
            row["bot_ref"], row["chat_ref"])
    if bot is None:
        return None
    token = box.decrypt(bot["token"],
                        box.aad("tg_bots", "token", bot["tenant_id"], bot["bot_id"]))
    return token, int(bot["chat_id"])


async def _sent(ids: list[int]) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE outbox SET state='sent', sent_at=now() WHERE id = ANY($1::bigint[])",
            ids)


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
        await conn.execute("DELETE FROM b24_call_log WHERE at < $1",
                           datetime.now(UTC) - CALL_LOG_RETENTION)
    await reminders.cleanup_marks()


async def main() -> None:
    s = get_settings()
    log_setup(s.log_level)
    await init_pool()
    log.info("воркер запущен")

    tick = 0
    next_stage_pass = datetime.now(UTC)
    next_lifecycle_pass = datetime.now(UTC)
    try:
        while True:
            tick += 1
            try:
                done = await process_events()
                sent = await send_outbox() + await flush_digests()
                heartbeat.beat("worker")
                # Проактивные сообщения: напоминания, эскалации, утренняя сводка.
                # Своё расписание у каждого прохода внутри, снаружи — один вызов.
                await reminders.run_due()
                if datetime.now(UTC) >= next_stage_pass:
                    # Отметка сдвигается ДО прохода: отказавший портал не должен
                    # превращать суточную синхронизацию в непрерывную.
                    next_stage_pass = datetime.now(UTC) + STAGE_PASS
                    await sync.sync_stages_due()
                if datetime.now(UTC) >= next_lifecycle_pass:
                    next_lifecycle_pass = datetime.now(UTC) + LIFECYCLE_PASS
                    await lifecycle.daily_pass()
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
