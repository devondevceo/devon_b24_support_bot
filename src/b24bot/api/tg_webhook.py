"""Приём апдейтов Telegram.

Инвариант И-1: сырой Update НЕ попадает в БД. Privacy mode у боевого бота выключен,
то есть в апдейтах едет вся переписка сотрудников клиента. Положить её в очередь —
значит положить в таблицу, в WAL и в бэкапы. Поэтому здесь из апдейта берутся только
идентификаторы, а тексты живут в памяти процесса до конца обработки.

Ответ ВСЕГДА 200 — и при неизвестном webhook_id, и при неверном секрете. Разница
только в метрике и в логе: иначе эндпоинт становится оракулом существования бота.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from b24bot.bot import dispatch
from b24bot.crypto import box
from b24bot.db.pool import pool, set_tenant, system_scope

log = logging.getLogger(__name__)
router = APIRouter(tags=["telegram"])

OK = JSONResponse({"ok": True})


@router.post("/tg/{webhook_id}")
async def receive(webhook_id: str, request: Request) -> JSONResponse:
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")

    # Резолв бота по недоверенному webhook_id — до сверки секрета теннант
    # неизвестен (RLS): это второй из резолверов docs/20-data-model.md §13.
    with system_scope():
        async with pool().acquire() as conn:
            bot = await conn.fetchrow(
                "SELECT id, tenant_id, bot_id, username, webhook_secret, status "
                "FROM tg_bots WHERE webhook_id = $1::uuid", webhook_id)

    if bot is None:
        log.warning("вебхук: неизвестный webhook_id")
        return OK

    expected = box.decrypt(
        bot["webhook_secret"],
        box.aad("tg_bots", "webhook_secret", bot["tenant_id"], bot["bot_id"]))
    if not hmac.compare_digest(expected, secret_header):
        log.warning("вебхук: неверный секрет, bot_id=%s", bot["bot_id"])
        return OK

    if bot["status"] not in ("active", "pending"):
        log.info("вебхук: бот %s в состоянии %s, апдейт отброшен",
                 bot["bot_id"], bot["status"])
        return OK

    try:
        update: dict[str, Any] = await request.json()
    except ValueError:
        log.warning("вебхук: тело не разобрано как JSON, bot_id=%s", bot["bot_id"])
        return OK

    set_tenant(int(bot["tenant_id"]))  # секрет сошёлся — запрос этого теннанта (RLS)
    try:
        await dispatch.handle(bot["id"], bot["tenant_id"], update)
    except Exception:
        # Ошибка обработки не должна приводить к повторной доставке: Telegram будет
        # слать этот апдейт снова и снова, пока не получит 200.
        log.exception("вебхук: ошибка обработки апдейта, bot_id=%s", bot["bot_id"])
    return OK
