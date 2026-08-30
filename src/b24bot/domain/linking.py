"""Привязка телеграм-аккаунта к пользователю Битрикс24 — обе двери в одну комнату.

Дверей две, и они несимметричны по тому, кто кого опознаёт:

* **портал → Telegram** (`_link_account` в боте): человек уже внутри Битрикса,
  портал сам прислал его `AUTH_ID`, мы знаем, кто он, и осталось узнать его
  телеграм. Одноразовый deep-link в бота это и делает;
* **Telegram → портал** (этот модуль, `begin`/`complete`): человек в чате
  поддержки, портал о нём пока ничего не знает. Бот даёт ссылку на экран
  согласия портала, человек входит там под собой, портал возвращает нам `code`,
  и `code` меняется на пару токенов.

Второе направление и есть то, ради чего модуль написан: у сотрудника клиента
Битрикс открыт далеко не всегда, а Telegram открыт всегда.

**Записывают обе двери одно и то же и одинаково** — `link_accounts()`. Разъедься
эти записи, привязка «из портала» и привязка «из чата» давали бы разное состояние
при одинаковом результате на экране, и найти это можно было бы только по жалобе.

Чего здесь намеренно нет — своей проверки прав. Кто человек в Битриксе, решает сам
Битрикс на своём экране входа; мы получаем ровно тот доступ, который у него есть.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from b24bot.b24 import oauth
from b24bot.b24.tokens import TokenStore
from b24bot.bot import texts
from b24bot.core.text import esc_html
from b24bot.db.pool import pool, set_tenant, system_scope
from b24bot.domain import access, audit, dm
from b24bot.domain.context import consume_token, issue_token

log = logging.getLogger(__name__)

# Вид токена состояния OAuth. Он не кнопка: в `callback_data` не уезжает, префикса
# в `bot/callbacks.py` не имеет и живёт в адресной строке браузера.
OAUTH_STATE = "oauth_state"
STATE_TTL = timedelta(minutes=15)

# `t.me/<бот>?start=link` — единственный способ начать привязку из ГРУППЫ, не
# показав личную ссылку всему чату. Кто нажал, тот и получит свою в личке.
START_ARG = "link"


@dataclass(frozen=True)
class Started:
    """Начало привязки: адрес экрана согласия и портал, куда он ведёт."""

    url: str
    portal_domain: str


@dataclass(frozen=True)
class Linked:
    """Итог привязки. `replaced_tg_user_id` — чью привязку этот вход отобрал."""

    tenant_id: int
    b24_user_id: int
    tg_user_id: int
    portal_domain: str
    display_name: str
    replaced_tg_user_id: int | None = None


@dataclass(frozen=True)
class Refusal:
    """Отказ. `code` — для логов и выбора текста, а не для показа как есть."""

    code: str


# ----------------------------------------------------------------------- начало
async def begin(tenant_id: int, tg_user_id: int, *, tg_username: str | None = None,
                display_name: str | None = None) -> Started | None:
    """Выдать личную ссылку на экран согласия портала. `None` — начать нечем.

    Ссылка личная в буквальном смысле: `state` — это разрешение записать в базу,
    что телеграм-аккаунт `tg_user_id` и есть тот, кто войдёт сейчас в портал.
    Поэтому она одноразовая, живёт четверть часа и отдаётся только в личку.

    Имя и `@username` едут в `payload` вместе с номером: обратно человек придёт
    в браузере, а там от Telegram нет ничего. Без них строка в `users` осталась
    бы безымянной, и экран «Команда» в приложении показывал бы пустые строки.
    Строку в `users` заранее не заводим: не дошедший до конца привязки человек
    не обязан оставлять по себе запись.
    """
    async with pool().acquire() as conn:
        domain = await conn.fetchval(
            "SELECT b24_domain FROM tenants WHERE id = $1 AND status = 'active'",
            tenant_id)
    if not domain:
        log.info("привязку начать нечем: у теннанта %s нет активного портала", tenant_id)
        return None

    state = await issue_token(
        OAUTH_STATE, tenant_id=tenant_id, ttl=STATE_TTL,
        payload={"tg_user_id": tg_user_id, "tg_username": tg_username,
                 "display_name": display_name})
    try:
        url = oauth.authorize_url(str(domain), state, redirect_uri=oauth.redirect_uri())
    except ValueError:
        log.warning("домен теннанта %s не прошёл проверку, ссылка не выдана", tenant_id)
        return None
    return Started(url=url, portal_domain=str(domain))


# ------------------------------------------------------------------------ итог
async def complete(state: str, code: str, *, domain_hint: str | None = None,
                   member_hint: str | None = None) -> Linked | Refusal:
    """Портал вернул `code`. Обменять, опознать человека, записать привязку.

    Порядок проверок — это порядок, в котором всё ломается: сначала наш `state`
    (без него неизвестно, чей это вход вообще), потом теннант, потом обмен, и
    только потом сверка того, что портал прислал, с тем, что мы о нём знаем.
    """
    # Поиск state — резолв недоверенного ввода, теннант ещё неизвестен (RLS).
    with system_scope():
        row = await consume_token(state, None)
    if row is None or row["kind"] != OAUTH_STATE or row["tenant_id"] is None:
        log.info("привязка отклонена: state не подошёл")
        return Refusal("state")

    raw = row["payload"]
    payload = json.loads(raw) if isinstance(raw, str) else raw
    tenant_id, tg_user_id = int(row["tenant_id"]), int(payload.get("tg_user_id") or 0)
    if not tg_user_id:
        return Refusal("state")
    set_tenant(tenant_id)  # дальше вся привязка — от имени этого теннанта

    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT b24_domain, b24_member_id FROM tenants "
            "WHERE id = $1 AND status = 'active'", tenant_id)
    if tenant is None:
        log.warning("привязка отклонена: теннант %s не активен", tenant_id)
        return Refusal("state")
    domain = str(tenant["b24_domain"])

    # Домен и member_id приходят в query-строке колбэка, то есть из браузера.
    # Своё знание ими не заменяется (И-4) — только сверяется с ними.
    if domain_hint and domain_hint != domain:
        log.warning("привязка отклонена: домен колбэка %r не совпал с порталом теннанта",
                    domain_hint[:64])
        return Refusal("mismatch")
    if member_hint and tenant["b24_member_id"] and member_hint != tenant["b24_member_id"]:
        log.warning("привязка отклонена: member_id колбэка не совпал с порталом теннанта")
        return Refusal("mismatch")

    data = await oauth.exchange_code(code)
    if not data or data.get("error") or not data.get("access_token"):
        log.warning("привязка отклонена: обмен кода не удался (%s)",
                    (data or {}).get("error", "нет ответа"))
        return Refusal("portal")

    # А вот member_id из ОТВЕТА обмена пришёл с oauth-хоста из allowlist по TLS —
    # это уже не подсказка браузера, а факт. Несовпадение означает, что код выдан
    # другим порталом, и связывать его с этим теннантом нельзя.
    if (data.get("member_id") and tenant["b24_member_id"]
            and str(data["member_id"]) != str(tenant["b24_member_id"])):
        log.warning("привязка отклонена: портал кода не тот, что у теннанта %s", tenant_id)
        return Refusal("mismatch")

    access_token = str(data["access_token"])
    refresh_token = str(data.get("refresh_token") or "")
    if not refresh_token:
        # Без refresh_token привязка мертва через час, а выглядит живой.
        log.warning("привязка отклонена: портал не прислал refresh_token")
        return Refusal("portal")

    me = await oauth.rest_call(domain, "user.current", access_token)
    result = me.get("result") or {}
    b24_user_id = int(result.get("ID") or 0)
    if not b24_user_id:
        log.warning("привязка отклонена: user.current не назвал пользователя")
        return Refusal("portal")
    if data.get("user_id") and int(data["user_id"]) != b24_user_id:
        # Два источника об одном и том же разошлись — писать наугад нельзя.
        log.error("привязка отклонена: user_id обмена %s не равен user.current %s",
                  data["user_id"], b24_user_id)
        return Refusal("mismatch")

    await TokenStore(pool()).store(
        tenant_id, b24_user_id, access_token, refresh_token, role="user",
        expires_in=int(data.get("expires_in") or 3600),
        authorized_tg_user_id=tg_user_id)

    replaced = await link_accounts(
        tenant_id, b24_user_id, tg_user_id,
        tg_username=payload.get("tg_username"),
        display_name=payload.get("display_name"))
    display = _display_name(result) or f"пользователь {b24_user_id}"

    # Ровно то же, что делает открытие приложения в портале: администратор портала
    # подтягивается до админа теннанта. Дверь другая — решение обязано быть одно,
    # иначе роль зависела бы от того, откуда человек пришёл.
    if (await oauth.is_portal_admin(domain, access_token)
            and await access.promote_portal_admin(tenant_id, b24_user_id)):
        await audit.record(tenant_id, "role.grant", actor_kind="system",
                           actor_id=b24_user_id, target=f"b24_user:{b24_user_id}",
                           detail={"причина": "администратор портала"})

    await audit.record(tenant_id, "user.map", actor_kind="user", actor_id=b24_user_id,
                       actor_tg_id=tg_user_id, target=f"b24_user:{b24_user_id}",
                       detail={"источник": "вход в портал из Telegram",
                               **({"отобрана у tg": replaced} if replaced else {})})
    log.info("привязка из Telegram: теннант %s, Б24 %s, TG %s",
             tenant_id, b24_user_id, tg_user_id)
    return Linked(tenant_id=tenant_id, b24_user_id=b24_user_id, tg_user_id=tg_user_id,
                  portal_domain=domain, display_name=display,
                  replaced_tg_user_id=replaced)


def _display_name(user: dict[str, Any]) -> str:
    parts = [user.get("NAME"), user.get("LAST_NAME")]
    return " ".join(str(p) for p in parts if p).strip()


async def notify_linked(result: Linked) -> None:
    """Сказать в Telegram обоим участникам итога.

    Второе сообщение важнее первого: человек, у которого привязку только что
    отобрали, иначе узнал бы об этом по отказу бота посреди работы — и решил бы,
    что сломались мы. Имя приходит из портала, поэтому экранируется (И-6).
    """
    token = await dm.bot_token(result.tenant_id)
    if token is None:
        return
    await dm.send(result.tenant_id, result.tg_user_id,
                  texts.MSG_LINK_OK.format(name=esc_html(result.display_name),
                                           b24_user_id=result.b24_user_id),
                  token=token)
    if result.replaced_tg_user_id:
        await dm.send(result.tenant_id, result.replaced_tg_user_id,
                      texts.MSG_LINK_TAKEN_OVER.format(b24_user_id=result.b24_user_id),
                      token=token)


# --------------------------------------------------------------------- запись
async def link_accounts(tenant_id: int, b24_user_id: int, tg_user_id: int, *,
                        tg_username: str | None = None,
                        display_name: str | None = None) -> int | None:
    """Единственное место, где привязка записывается. Обе двери ведут сюда.

    Возвращает телеграм-аккаунт, у которого этот пользователь Битрикса только что
    отобран, если такой был. Отбирать приходится: `b24_user_tokens` хранит ровно
    один `authorized_tg_user_id`, и без снятия старой строки в `tenant_members`
    она осталась бы `authorized` при мёртвом доступе — то самое расхождение,
    которое человек видит как «бот меня не узнаёт», а мы в базе не видим вовсе.
    """
    async with pool().acquire() as conn, conn.transaction():
        user_row = await conn.fetchrow(
            "INSERT INTO users (tg_user_id, tg_username, display_name) VALUES ($1,$2,$3) "
            "ON CONFLICT (tg_user_id) DO UPDATE SET "
            "  tg_username = COALESCE(EXCLUDED.tg_username, users.tg_username), "
            "  display_name = COALESCE(EXCLUDED.display_name, users.display_name) "
            "RETURNING id", tg_user_id, tg_username, display_name)
        user_id = int(user_row["id"])

        replaced = await conn.fetchval(
            "UPDATE tenant_members m SET link_status = 'revoked' "
            "FROM users u WHERE u.id = m.user_id AND m.tenant_id = $1 "
            "  AND m.b24_user_id = $2 AND m.user_id <> $3 "
            "  AND m.link_status = 'authorized' "
            "RETURNING u.tg_user_id", tenant_id, b24_user_id, user_id)

        await conn.execute(
            "INSERT INTO tenant_members (tenant_id, user_id, role, b24_user_id, "
            "link_status, linked_at) VALUES ($1,$2,'member',$3,'authorized',now()) "
            "ON CONFLICT (tenant_id, user_id) DO UPDATE SET "
            "  b24_user_id = EXCLUDED.b24_user_id, link_status = 'authorized', "
            "  linked_at = now()", tenant_id, user_id, b24_user_id)

        # Токен отдаётся только тому, кто его авторизовал (docs/40-security.md §2).
        await conn.execute(
            "UPDATE b24_user_tokens SET authorized_tg_user_id = $3 "
            "WHERE tenant_id = $1 AND b24_user_id = $2", tenant_id, b24_user_id, tg_user_id)

    return int(replaced) if replaced is not None else None
