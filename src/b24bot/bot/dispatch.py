"""Разбор апдейта Telegram. Общий код для двух транспортов.

Инвариант И-1: сырой Update не попадает в БД. Из апдейта берутся только
идентификаторы; тексты живут в памяти процесса до конца обработки.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any

from b24bot.bot import handlers
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.tg import api as tg

log = logging.getLogger(__name__)


def chat_of(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message", "my_chat_member", "chat_member"):
        node = update.get(key)
        if isinstance(node, dict) and isinstance(node.get("chat"), dict):
            chat: dict[str, Any] = node["chat"]
            return chat
    cb = update.get("callback_query")
    if isinstance(cb, dict) and isinstance(cb.get("message"), dict):
        cb_chat = cb["message"].get("chat")
        if isinstance(cb_chat, dict):
            found: dict[str, Any] = cb_chat
            return found
    return None


async def handle(bot_ref: int, tenant_id: int | None, update: dict[str, Any]) -> None:
    """Минимальная обработка: регистрация чата и учёт присутствия бота.

    Сценарии создания задач появятся отдельно. Здесь только то, без чего невозможно
    ничего дальше: узнать, в каких чатах бот вообще находится.
    """
    chat = chat_of(update)
    if chat is None:
        return

    chat_id = int(chat["id"])
    chat_type = str(chat.get("type") or "group")
    title = str(chat.get("title") or chat.get("username") or "")
    is_forum = bool(chat.get("is_forum"))

    if chat_type not in ("group", "supergroup"):
        return

    left = False
    my_member = update.get("my_chat_member")
    if isinstance(my_member, dict):
        status = str((my_member.get("new_chat_member") or {}).get("status") or "")
        left = status in ("left", "kicked")

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tg_chats (chat_id, tenant_id, bot_ref, type, title, is_forum, status)
            VALUES ($1, NULL, $2, $3, $4, $5, 'unclaimed')
            ON CONFLICT (chat_id) WHERE status <> 'migrated'
            DO UPDATE SET title = EXCLUDED.title,
                          is_forum = EXCLUDED.is_forum,
                          bot_ref = EXCLUDED.bot_ref,
                          type = EXCLUDED.type,
                          -- Бота вернули в чат: снимаем метку 'left', иначе чат
                          -- остаётся мёртвым навсегда. Привязка при этом не теряется.
                          status = CASE
                              WHEN tg_chats.status = 'left' AND tg_chats.tenant_id IS NULL
                                  THEN 'unclaimed'
                              WHEN tg_chats.status = 'left'
                                  THEN 'active'
                              ELSE tg_chats.status
                          END
            RETURNING id, tenant_id, status
            """,
            chat_id, bot_ref, chat_type, title, is_forum)

        if left and row is not None:
            await conn.execute("UPDATE tg_chats SET status = 'left' WHERE id = $1", row["id"])
            log.info("бот удалён из чата chat_ref=%s", row["id"])
            return

    log.info("апдейт: chat_ref=%s chat_id=%s type=%s forum=%s состояние=%s",
             row["id"] if row else "?", chat_id, chat_type, is_forum,
             row["status"] if row else "?")


async def _bot_row(bot_ref: int) -> dict[str, Any] | None:
    async with pool().acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, tenant_id, bot_id, username, token FROM tg_bots WHERE id = $1",
            bot_ref)
    if r is None:
        return None
    row: dict[str, Any] = {
        "id": r["id"], "tenant_id": r["tenant_id"], "bot_id": r["bot_id"],
        "username": r["username"],
        "token": box.decrypt(r["token"],
                             box.aad("tg_bots", "token", r["tenant_id"], r["bot_id"])),
    }
    return row


async def route(bot_ref: int, update: dict[str, Any]) -> None:
    """Сценарии бота. Вызывается после регистрации чата."""
    bot = await _bot_row(bot_ref)
    if bot is None:
        return

    reply = None
    target_chat = target_thread = edit_message_id = None
    try:
        if "callback_query" in update:
            cb = update["callback_query"]
            reply = await handlers.on_callback(bot, cb)
            msg = cb.get("message") or {}
            target_chat = (msg.get("chat") or {}).get("id")
            target_thread = msg.get("message_thread_id")
            edit_message_id = msg.get("message_id")
            with contextlib.suppress(tg.TelegramError):
                await tg.call(bot["token"], "answerCallbackQuery",
                              {"callback_query_id": cb.get("id")})
        elif "message" in update:
            msg = update["message"]
            reply = await handlers.on_message(bot, msg)
            target_chat = (msg.get("chat") or {}).get("id")
            target_thread = msg.get("message_thread_id")
    except Exception:
        log.exception("сценарий упал на апдейте %s", update.get("update_id"))
        return

    if reply is None or target_chat is None:
        return
    try:
        if reply.edit and edit_message_id:
            # Список, сводка и карточка живут в одном сообщении: редактируем его,
            # а не плодим новые. «message is not modified» — не ошибка.
            try:
                await tg.call(bot["token"], "editMessageText", {
                    "chat_id": int(target_chat), "message_id": edit_message_id,
                    "text": reply.text, "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": reply.markup})
                return
            except tg.TelegramError as exc:
                if "not modified" in exc.description:
                    return
                log.info("редактирование не прошло (%s), шлю новым сообщением", exc.code)
        sent = await tg.send_message(bot["token"], int(target_chat), reply.text,
                                     thread_id=target_thread,
                                     reply_markup=reply.markup)
        if reply.remember_for_survey and sent.get("message_id"):
            # Опросник принимает ответ только реплаем на СВОЙ вопрос.
            from b24bot.bot import survey
            await survey.remember_question(reply.remember_for_survey,
                                           int(sent["message_id"]))
    except tg.TelegramError as exc:
        log.warning("не удалось отправить ответ в чат %s: %s", target_chat, exc)


# В состоянии unclaimed бот молчит и ничего сверх регистрации не сохраняет:
# иначе брендированный бот теннанта становится бесплатной рассылкой по чужим
# группам и сборщиком ПДн (docs/40-security.md §11).
