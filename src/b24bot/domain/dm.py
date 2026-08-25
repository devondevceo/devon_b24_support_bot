"""Личное сообщение от бота — единственная дорога в обход `outbox`.

Очередь `outbox` жёстко привязана к `tg_chats`, а личных чатов там нет и не
будет: `dispatch.py` их принципиально не регистрирует (иначе брендированный бот
теннанта превращается в бесплатную рассылку по чужим группам). Поэтому всё, что
адресовано человеку лично — запрос на подтверждение, напоминание о сроке, —
отправляется отсюда напрямую, синхронно, без ретраев.

**Недоставка здесь — нормальный ответ, а не сбой.** Пока человек ни разу не
написал боту, Telegram на любое сообщение ему отвечает 400/403: у бота нет права
начать диалог первым. Это ровно та же правда, что «письмо на несуществующий
адрес», и вызывающий обязан её увидеть — поэтому функция возвращает `bool`, а не
молчит. Тому, что зависит от доставки, положено эскалировать в чат
(`domain/reminders.py`), а не ждать вечно.

Прокси здесь не при чём: SOCKS5 к Telegram живёт на уровне httpx-клиента
(`tg/api.py`), а не в аргументах вызова.
"""
from __future__ import annotations

import logging
from typing import Any

from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.tg import api as tg

log = logging.getLogger(__name__)


async def bot_token(tenant_id: int) -> str | None:
    """Токен бота теннанта в открытом виде. `None` — бот не подключён.

    В логи не попадает никогда: httpx печатает полный URL на уровне INFO, а токен
    лежит прямо в пути (инвариант И-7, `core/logging.py`).
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT bot_id, token FROM tg_bots WHERE tenant_id = $1", tenant_id)
    if row is None:
        return None
    token = box.decrypt(row["token"],
                        box.aad("tg_bots", "token", tenant_id, row["bot_id"]))
    return str(token)


async def send(tenant_id: int, tg_user_id: int, text: str, *,
               markup: dict[str, Any] | None = None,
               token: str | None = None) -> bool:
    """Отправить личное сообщение. `False` — не доставлено, и это знание нужное.

    `token` передают там, где он уже расшифрован и адресатов несколько: лишняя
    расшифровка на каждого — это лишний поход в базу и лишняя работа с секретом.
    """
    real_token = token or await bot_token(tenant_id)
    if real_token is None:
        log.warning("личное сообщение не отправлено: у теннанта %s не подключён бот",
                    tenant_id)
        return False
    try:
        await tg.send_message(real_token, tg_user_id, text, reply_markup=markup)
    except tg.TelegramError as exc:
        # 400/403 — человек не открывал диалог с ботом либо заблокировал его.
        log.info("личное сообщение теннанта %s пользователю %s не доставлено: %s",
                 tenant_id, tg_user_id, exc)
        return False
    return True
