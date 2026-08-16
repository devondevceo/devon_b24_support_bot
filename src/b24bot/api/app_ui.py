"""Интерфейс приложения внутри Битрикс24.

Здесь теннант настраивает интеграцию, не выходя из портала. По решению заказчика
именно тут вводится токен Telegram-бота — не в чат и не в отдельную панель.

Безопасность страницы:
  * форма подписана короткоживущей сессией, привязанной к (теннант, пользователь Б24);
    наружу уходит случайное значение, в БД лежит его sha256;
  * токен бота принимает ТОЛЬКО администратор портала;
  * ни токен, ни URL вебхука никогда не показываются обратно;
  * все подстановки проходят через esc_html (инвариант И-6).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from b24bot.core.config import get_settings, is_trusted_portal_domain
from b24bot.core.text import esc_attr, esc_html
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.domain import context, miniapp
from b24bot.tg import api as tg

log = logging.getLogger(__name__)
router = APIRouter(prefix="/b24/app", tags=["bitrix24-app"])

SESSION_TTL = timedelta(minutes=30)


# --------------------------------------------------------------------- сессии
def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def issue_session(conn: asyncpg.Connection, tenant_id: int, b24_user_id: int,
                        is_portal_admin: bool) -> str:
    token = secrets.token_urlsafe(16)
    await conn.execute(
        "INSERT INTO app_sessions (token_hash, tenant_id, b24_user_id, is_portal_admin, "
        "expires_at) VALUES ($1, $2, $3, $4, $5)",
        _hash(token), tenant_id, b24_user_id, is_portal_admin,
        datetime.now(UTC) + SESSION_TTL)
    return token


async def load_session(token: str) -> asyncpg.Record | None:
    async with pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT tenant_id, b24_user_id, is_portal_admin FROM app_sessions "
            "WHERE token_hash = $1 AND expires_at > now()", _hash(token))


# ---------------------------------------------------------------------- вёрстка
STYLE = """
 body{font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:20px;
      color:#1a1a1a;background:#fff}
 .card{max-width:680px;border:1px solid #e3e5e7;border-radius:10px;padding:18px 20px;
       margin:0 0 14px}
 h1{font-size:17px;margin:0 0 14px} h2{font-size:14px;margin:0 0 10px;color:#525c69}
 code{background:#f4f5f6;padding:1px 5px;border-radius:4px;font-size:12px}
 .row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #f2f3f5}
 .row:last-child{border-bottom:0} .row span:first-child{color:#828b95}
 .ok{color:#1f8b4c} .warn{color:#b58200} .err{color:#c8332e}
 input[type=text]{width:100%;padding:9px 11px;border:1px solid #d5d7db;border-radius:6px;
                  font:13px/1.4 monospace;box-sizing:border-box}
 button{background:#2066b0;color:#fff;border:0;border-radius:6px;padding:9px 18px;
        font-size:14px;cursor:pointer;margin-top:10px}
 button.sec{background:#eaecef;color:#1a1a1a}
 .hint{color:#828b95;font-size:12px;margin-top:8px}
 .msg{padding:10px 12px;border-radius:6px;margin:0 0 14px}
 .msg.ok{background:#eaf6ee} .msg.err{background:#fdecec} .msg.warn{background:#fdf6e3}
 .chat{padding:12px 0;border-bottom:1px solid #eef0f2} .chat:last-of-type{border-bottom:0}
 .chat-head{display:flex;justify-content:space-between;align-items:baseline;margin:0 0 8px}
 .chat-name{font-weight:600;font-size:15px}
 .proj{display:flex;justify-content:space-between;align-items:center;
       padding:5px 0 5px 14px;border-left:2px solid #e3e5e7;margin:0 0 2px}
 .muted{color:#828b95;font-weight:400}
 .link-btn{background:none;border:0;color:#2066b0;cursor:pointer;padding:0;margin:0;
           font-size:12px;text-decoration:underline}
 .bind{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin:10px 0 0}
 .bind label{display:flex;flex-direction:column;font-size:12px;color:#828b95;gap:3px}
 .bind select{padding:7px 9px;border:1px solid #d5d7db;border-radius:6px;font-size:13px;
              max-width:260px}
 .bind button{margin:0;padding:8px 16px}
"""


def page(body: str, portal_domain: str | None, *, title: str = "Поддержка в Telegram"
         ) -> HTMLResponse:
    html = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc_html(title)}</title>"
            f'<script src="//api.bitrix24.com/api/v1/"></script>'
            f"<style>{STYLE}</style></head><body>{body}"
            f"<script>BX24.init(function(){{BX24.fitWindow();}});</script>"
            f"</body></html>")
    resp = HTMLResponse(html)
    # frame-ancestors — динамически, только под домен проверенного портала.
    if portal_domain and is_trusted_portal_domain(portal_domain):
        resp.headers["Content-Security-Policy"] = f"frame-ancestors https://{portal_domain}"
    else:
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return resp


def _row(label: str, value: str, css: str = "") -> str:
    cls = f' class="{css}"' if css else ""
    return f"<div class='row'><span>{esc_html(label)}</span><span{cls}>{value}</span></div>"


async def render_home(tenant: asyncpg.Record, b24_user_id: int, is_admin: bool,
                      session: str, *, message: str = "", message_kind: str = "ok") -> str:
    """Главный экран приложения."""
    async with pool().acquire() as conn:
        bot = await conn.fetchrow(
            "SELECT bot_id, username, status, mode, privacy_mode_off, last_check_at, "
            "last_error, miniapp_short_name FROM tg_bots WHERE tenant_id = $1",
            tenant["id"])
        counts = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM clients WHERE tenant_id = $1) AS clients, "
            "       (SELECT count(*) FROM projects WHERE tenant_id = $1 "
            "        AND status = 'active') AS projects, "
            "       (SELECT count(*) FROM chat_bindings WHERE tenant_id = $1 "
            "        AND status = 'active') AS bindings",
            tenant["id"])

    msg_html = (f"<div class='msg {message_kind}'>{message}</div>") if message else ""

    # ------------------------------------------------------ статус интеграции
    scopes = len(tenant["granted_scope"] or [])
    status_rows = (
        _row("Портал", f"<code>{esc_html(tenant['b24_domain'])}</code>")
        + _row("Установка", "<span class='ok'>завершена</span>"
               if tenant["install_state"] == "finished"
               else f"<span class='warn'>{esc_html(tenant['install_state'])}</span>")
        + _row("Выдано прав", str(scopes))
        + _row("Клиентов", str(counts["clients"]))
        + _row("Проектов", str(counts["projects"]))
        + _row("Привязанных чатов", str(counts["bindings"]))
    )

    # --------------------------------------------------------------- бот
    if bot:
        privacy = bot["privacy_mode_off"]
        privacy_html = {
            True: "<span class='ok'>выключен, как надо</span>",
            False: "<span class='err'>ВКЛЮЧЁН — бот не увидит сообщения в группах</span>",
            None: "<span class='warn'>не проверено</span>",
        }[privacy]
        status_html = {
            "active": "<span class='ok'>работает</span>",
            "pending": "<span class='warn'>подключается</span>",
            "error": f"<span class='err'>ошибка: {esc_html(bot['last_error'] or '')}</span>",
            "suspended": "<span class='err'>приостановлен</span>",
        }.get(bot["status"], esc_html(bot["status"]))

        bot_rows = (
            _row("Бот", f"<code>@{esc_html(bot['username'])}</code>")
            + _row("Состояние", status_html)
            + _row("Режим приёма", "long polling через прокси"
                   if bot["mode"] == "polling" else "вебхук")
            + _row("Privacy mode", privacy_html)
            + _row("Проверен", bot["last_check_at"].strftime("%d.%m.%Y %H:%M")
                   if bot["last_check_at"] else "—")
        )
        bot_form = (
            f"{bot_rows}"
            f"<form method='post' action='/b24/app/bot'>"
            f"<input type='hidden' name='session' value='{esc_attr(session)}'>"
            f"<input type='hidden' name='action' value='recheck'>"
            f"<button class='sec' type='submit'>Проверить подключение</button>"
            f"</form>"
            if is_admin else bot_rows)
    else:
        bot_form = (
            "<p>Бот пока не подключён. Создайте его в "
            "<code>@BotFather</code> и вставьте токен сюда.</p>"
            f"<form method='post' action='/b24/app/bot'>"
            f"<input type='hidden' name='session' value='{esc_attr(session)}'>"
            f"<input type='hidden' name='action' value='save'>"
            f"<input type='text' name='token' placeholder='123456789:AA...' "
            f"autocomplete='off' spellcheck='false'>"
            f"<button type='submit'>Подключить бота</button>"
            f"</form>"
            "<div class='hint'>В BotFather обязательно выполните "
            "<code>/setprivacy</code> → выберите бота → <b>Disable</b>. "
            "Иначе бот в группе видит только команды и упоминания, и создать задачу "
            "ответом на сообщение коллеги будет нельзя.</div>"
            if is_admin else
            "<p>Бот не подключён. Обратитесь к администратору портала.</p>")

    link_block = await _link_block(tenant, b24_user_id, bot)
    chats_block = await _chats_block(tenant, b24_user_id, is_admin, session)
    app_block = _miniapp_block(bot, is_admin, session)

    admin_note = "" if is_admin else (
        "<div class='hint'>Вы вошли как обычный пользователь. Настройки доступны "
        "администратору портала.</div>")

    return (
        f"{msg_html}"
        f"<div class='card'><h1>Поддержка в Telegram</h1>"
        f"<h2>Интеграция</h2>{status_rows}{admin_note}</div>"
        f"<div class='card'><h2>Telegram-бот</h2>{bot_form}</div>"
        f"{app_block}"
        f"<div class='card'><h2>Чаты</h2>{chats_block}</div>"
        f"<div class='card'><h2>Вы</h2>"
        f"{_row('Пользователь Битрикс24', f'<code>{b24_user_id}</code>')}"
        f"{_row('Права', 'администратор портала' if is_admin else 'сотрудник')}"
        f"{link_block}</div>")


def _miniapp_block(bot: asyncpg.Record | None, is_admin: bool, session: str) -> str:
    """Состояние мини-аппа. Настраивать здесь почти нечего — и это осознанно.

    Кнопка меню бота ставится сама при подключении и при проверке. Единственное
    ручное действие во всей цепочке — `/newapp` в BotFather, и оно НЕ обязательно:
    без него из группы открывается на касание длиннее, через личку бота.
    """
    if bot is None:
        return ""
    url = miniapp.web_app_url()
    if url is None:
        return ("<div class='card'><h2>Приложение в Telegram</h2>"
                "<p class='hint'>Публичный адрес сервиса не по https — приложение "
                "отключено.</p></div>")

    short = str(bot["miniapp_short_name"] or "")
    rows = (
        _row("Адрес", f"<code>{esc_html(url)}</code>")
        + _row("Кнопка меню бота", "<span class='ok'>ставится автоматически</span>")
        + _row("Открытие из группы",
               "<span class='ok'>в одно касание</span>" if short else
               "<span class='warn'>через личку бота</span>")
    )

    if not is_admin:
        return f"<div class='card'><h2>Приложение в Telegram</h2>{rows}</div>"

    form = (
        f"<form method='post' action='/b24/app/miniapp'>"
        f"<input type='hidden' name='session' value='{esc_attr(session)}'>"
        f"<input type='text' name='short_name' value='{esc_attr(short)}' "
        f"placeholder='например tasks' autocomplete='off' spellcheck='false'>"
        f"<button type='submit'>Сохранить имя приложения</button>"
        f"</form>"
        "<div class='hint'>Необязательно. Если в BotFather выполнить "
        "<code>/newapp</code> и указать адрес выше, кнопка в групповом чате будет "
        "открывать приложение сразу. Без этого она сначала ведёт в личку бота — "
        "работает точно так же, просто на касание больше.</div>")
    return f"<div class='card'><h2>Приложение в Telegram</h2>{rows}{form}</div>"


@router.post("/miniapp")
async def save_miniapp(session: str = Form(...), short_name: str = Form("")) -> HTMLResponse:
    sess = await load_session(session)
    if sess is None:
        return page("<div class='card'><h1>Сессия истекла</h1>"
                    "<p>Закройте и откройте приложение заново.</p></div>", None)

    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants WHERE id = $1",
            sess["tenant_id"])

    if not sess["is_portal_admin"]:
        body = await render_home(tenant, sess["b24_user_id"], False, session,
                                 message="Настраивать приложение может только "
                                         "администратор портала.", message_kind="err")
        return page(body, tenant["b24_domain"])

    value = short_name.strip()
    if value and not miniapp.SHORT_NAME_RE.match(value):
        message, kind = ("Имя приложения из BotFather: латиница, цифры и знак "
                         "подчёркивания, от 3 до 30 символов.", "err")
    else:
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE tg_bots SET miniapp_short_name = $2, updated_at = now() "
                "WHERE tenant_id = $1", tenant["id"], value or None)
        message, kind = (("Имя приложения сохранено — из групп будет открываться "
                          "сразу.", "ok") if value else
                         ("Имя приложения убрано: из групп приложение открывается "
                          "через личку бота.", "ok"))

    async with pool().acquire() as conn:
        fresh = await issue_session(conn, tenant["id"], sess["b24_user_id"], True)
    body = await render_home(tenant, sess["b24_user_id"], True, fresh,
                             message=message, message_kind=kind)
    return page(body, tenant["b24_domain"])


async def _portal_projects(tenant_id: int, b24_user_id: int
                           ) -> tuple[list[dict[str, Any]], str]:
    """Проекты портала, доступные ЭТОМУ пользователю.

    sonet_group.user.groups возвращает группы владельца токена вместе с его ролью.
    Ходим его личным токеном — значит видим ровно то, что видит он сам, и не
    показываем проекты, к которым у человека нет доступа.
    """
    from b24bot.b24 import errors
    from b24bot.domain import access

    try:
        client = await access.client_for_user(tenant_id, b24_user_id)
        async with client:
            groups = await client.call("sonet_group.user.groups", {})
    except errors.B24Error as exc:
        log.warning("не удалось получить проекты портала: %s", exc)
        return [], "Битрикс24 не ответил, список проектов может быть неполным."
    except Exception as exc:
        log.warning("не удалось получить проекты портала: %s", str(exc)[:150])
        return [], "Битрикс24 не ответил, список проектов может быть неполным."

    out: list[dict[str, Any]] = []
    for g in groups or []:
        gid = g.get("GROUP_ID") or g.get("ID")
        if gid is None:
            continue
        out.append({"id": int(gid),
                    "name": str(g.get("GROUP_NAME") or g.get("NAME") or gid),
                    "role": str(g.get("ROLE") or ""),
                    "extranet": g.get("IS_EXTRANET") == "Y"})
    out.sort(key=lambda x: x["name"].lower())
    return out, ""


async def _chats_block(tenant: asyncpg.Record, b24_user_id: int, is_admin: bool,
                       session: str) -> str:
    """Чаты, где присутствует бот теннанта, и их привязки к проектам.

    Показываем только свои чаты и ничейные, увиденные НАШИМ ботом. Чужой чат сюда
    не попадает: бот другого теннанта его нам не покажет, а ручной ввод chat_id
    запрещён (docs/40-security.md §4).
    """
    async with pool().acquire() as conn:
        chats = await conn.fetch(
            """
            SELECT c.id, c.chat_id, c.title, c.status, c.is_forum
              FROM tg_chats c
              LEFT JOIN tg_bots b ON b.id = c.bot_ref
             WHERE c.status <> 'migrated'
               AND (c.tenant_id = $1 OR (c.tenant_id IS NULL AND b.tenant_id = $1))
             ORDER BY c.first_seen_at
            """, tenant["id"])
        bindings = await conn.fetch(
            """
            SELECT b.id, b.chat_ref, b.project_id, p.name AS project,
                   p.b24_group_id, c.id AS client_id, c.name AS client
              FROM chat_bindings b
              JOIN projects p ON p.id = b.project_id
              JOIN clients  c ON c.id = p.client_id
             WHERE b.tenant_id = $1 AND b.status = 'active'
             ORDER BY p.name
            """, tenant["id"])
        clients = await conn.fetch(
            "SELECT id, name FROM clients WHERE tenant_id = $1 AND status = 'active' "
            "ORDER BY name", tenant["id"])
        known = await conn.fetch(
            "SELECT b24_group_id, name FROM projects "
            "WHERE tenant_id = $1 AND status = 'active'", tenant["id"])

    portal, portal_error = await _portal_projects(int(tenant["id"]), b24_user_id)
    known_ids = {int(k["b24_group_id"]) for k in known}

    if not chats:
        return ("<p>Бот пока не добавлен ни в один чат.</p>"
                "<div class='hint'>Добавьте бота в групповой чат Telegram — чат появится "
                "здесь сам. Вводить идентификатор чата вручную нельзя: принадлежность "
                "подтверждается фактом присутствия бота.</div>")

    by_chat: dict[int, list[asyncpg.Record]] = {}
    for b in bindings:
        by_chat.setdefault(b["chat_ref"], []).append(b)

    state = {
        "unclaimed": ("не привязан", "warn"),
        "claimed": ("подключён", "ok"),
        "active": ("работает", "ok"),
        "left": ("бот удалён из чата", "err"),
    }

    out = []
    for ch in chats:
        label, css = state.get(ch["status"], (ch["status"], ""))
        chat_name = ch["title"] or f"чат {ch['chat_id']}"
        head = (f"<div class='chat-head'>"
                f"<span class='chat-name'>{esc_html(chat_name)}</span>"
                f"<span class='{css}'>{esc_html(label)}</span></div>")

        linked = by_chat.get(ch["id"], [])
        items = []
        for b in linked:
            btn = ""
            if is_admin:
                btn = (
                    "<form method='post' action='/b24/app/chat' style='display:inline'>"
                    f"<input type='hidden' name='session' value='{esc_attr(session)}'>"
                    "<input type='hidden' name='action' value='unbind'>"
                    f"<input type='hidden' name='binding_id' value='{b['id']}'>"
                    "<button class='link-btn' type='submit'>отвязать</button></form>")
            items.append(
                f"<div class='proj'><span><b>{esc_html(b['project'])}</b>"
                f"<span class='muted'> · клиент {esc_html(b['client'])}</span></span>"
                f"{btn}</div>")
        if not linked:
            items.append("<div class='proj muted'>проектов пока нет</div>")

        form = ""
        if is_admin and ch["status"] != "left":
            form = _bind_form(ch, linked, portal, known_ids, clients, session)

        out.append(f"<div class='chat'>{head}{''.join(items)}{form}</div>")

    err = (f"<div class='hint err'>{esc_html(portal_error)}</div>" if portal_error else "")
    hint = ("" if is_admin else
            "<div class='hint'>Управлять привязками может администратор портала.</div>")
    return "".join(out) + err + hint


def _bind_form(chat: asyncpg.Record, linked: list[asyncpg.Record],
               portal: list[dict[str, Any]], known_ids: set[int],
               clients: list[asyncpg.Record], session: str) -> str:
    """Привязка чата к проекту портала.

    Клиент выбирается ЯВНО и всегда. Раньше он подставлялся из существующих привязок
    чата, из-за чего новый проект молча уезжал под чужого клиента.
    """
    bound = {int(b["b24_group_id"]) for b in linked if b["b24_group_id"]}
    options = "".join(
        f"<option value='{g['id']}'>{esc_html(g['name'])}"
        f"{'' if g['id'] in known_ids else ' — новый'}</option>"
        for g in portal if g["id"] not in bound)
    if not options:
        return "<div class='hint'>Все доступные вам проекты уже привязаны к этому чату.</div>"

    client_opts = "".join(f"<option value='{c['id']}'>{esc_html(c['name'])}</option>"
                          for c in clients)
    return (
        "<form method='post' action='/b24/app/chat' class='bind'>"
        f"<input type='hidden' name='session' value='{esc_attr(session)}'>"
        "<input type='hidden' name='action' value='bind'>"
        f"<input type='hidden' name='chat_ref' value='{chat['id']}'>"
        f"<label>Проект портала<select name='b24_group_id'>{options}</select></label>"
        f"<label>Клиент<select name='client_id'>"
        "<option value='0'>создать по названию проекта</option>"
        f"{client_opts}</select></label>"
        "<button type='submit'>Привязать</button></form>"
        "<div class='hint'>Один чат обслуживает одного клиента: все проекты чата "
        "должны принадлежать ему.</div>")


async def _link_block(tenant: asyncpg.Record, b24_user_id: int,
                      bot: asyncpg.Record | None) -> str:
    """Привязка Telegram — ОСНОВНОЙ путь сопоставления.

    Телефон заполнен у 5 сотрудников из 27, поэтому сопоставление по нему как главный
    механизм нежизнеспособно. Здесь мы уже знаем, кто человек: портал сам прислал его
    токен. Остаётся связать это с его telegram-аккаунтом одноразовой ссылкой.
    """
    if bot is None:
        return ("<div class='hint'>Привязка Telegram станет доступна, когда "
                "администратор подключит бота.</div>")

    async with pool().acquire() as conn:
        linked = await conn.fetchrow(
            "SELECT u.tg_username, m.link_status FROM tenant_members m "
            "JOIN users u ON u.id = m.user_id "
            "WHERE m.tenant_id = $1 AND m.b24_user_id = $2", tenant["id"], b24_user_id)

    if linked and linked["link_status"] == "authorized":
        who = f"@{esc_html(linked['tg_username'])}" if linked["tg_username"] else "привязан"
        return (_row("Telegram", f"<span class='ok'>{who}</span>")
                + "<div class='hint'>Вы можете создавать и редактировать задачи из чатов.</div>")

    token = await context.issue_token(
        "link", tenant_id=tenant["id"], payload={"b24_user_id": b24_user_id},
        ttl=timedelta(minutes=15))
    url = f"https://t.me/{esc_attr(bot['username'])}?start=b{esc_attr(token)}"
    return (_row("Telegram", "<span class='warn'>не привязан</span>")
            + f"<a href='{url}' target='_blank' rel='noopener'>"
            f"<button type='button'>Привязать Telegram</button></a>"
            "<div class='hint'>Ссылка одноразовая и живёт 15 минут. "
            "Откроется чат с ботом — нажмите «Запустить».</div>")


# ------------------------------------------------------------------ сохранение
@router.post("/bot")
async def save_bot(request: Request, session: str = Form(...), action: str = Form("save"),
                   token: str = Form("")) -> HTMLResponse:
    sess = await load_session(session)
    if sess is None:
        return page("<div class='card'><h1>Сессия истекла</h1>"
                    "<p>Закройте и откройте приложение заново.</p></div>", None)

    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants WHERE id = $1",
            sess["tenant_id"])
    domain = tenant["b24_domain"]

    if not sess["is_portal_admin"]:
        log.warning("попытка настройки бота не-администратором: tenant=%s user=%s",
                    sess["tenant_id"], sess["b24_user_id"])
        body = await render_home(tenant, sess["b24_user_id"], False, session,
                                 message="Настраивать бота может только администратор "
                                         "портала.", message_kind="err")
        return page(body, domain)

    message, kind = "", "ok"

    if action == "save":
        message, kind = await _connect_bot(tenant, token.strip())
    elif action == "recheck":
        message, kind = await _recheck_bot(tenant)

    async with pool().acquire() as conn:
        fresh = await issue_session(conn, tenant["id"], sess["b24_user_id"], True)
    body = await render_home(tenant, sess["b24_user_id"], True, fresh,
                             message=message, message_kind=kind)
    return page(body, domain)


@router.post("/chat")
async def chat_action(session: str = Form(...), action: str = Form(...),
                      chat_ref: int = Form(0), b24_group_id: int = Form(0),
                      client_id: int = Form(0),
                      binding_id: int = Form(0)) -> HTMLResponse:
    sess = await load_session(session)
    if sess is None:
        return page("<div class='card'><h1>Сессия истекла</h1>"
                    "<p>Закройте и откройте приложение заново.</p></div>", None)

    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants WHERE id = $1",
            sess["tenant_id"])

    if not sess["is_portal_admin"]:
        body = await render_home(tenant, sess["b24_user_id"], False, session,
                                 message="Управлять привязками может только администратор "
                                         "портала.", message_kind="err")
        return page(body, tenant["b24_domain"])

    message, kind = await _apply_chat_action(
        int(tenant["id"]), int(sess["b24_user_id"]), action, chat_ref,
        b24_group_id, client_id, binding_id)

    async with pool().acquire() as conn:
        fresh = await issue_session(conn, tenant["id"], sess["b24_user_id"], True)
    body = await render_home(tenant, sess["b24_user_id"], True, fresh,
                             message=message, message_kind=kind)
    return page(body, tenant["b24_domain"])


async def _apply_chat_action(tenant_id: int, b24_user_id: int, action: str,
                             chat_ref: int, b24_group_id: int, client_id: int,
                             binding_id: int) -> tuple[str, str]:
    async with pool().acquire() as conn:
        if action == "unbind":
            row = await conn.fetchrow(
                "UPDATE chat_bindings SET status = 'disabled' "
                "WHERE id = $1 AND tenant_id = $2 RETURNING chat_ref",
                binding_id, tenant_id)
            return ("Привязка снята.", "ok") if row else ("Привязка не найдена.", "err")

        if action != "bind":
            return ("Неизвестное действие.", "err")

        owner = await conn.fetchval(
            "SELECT tenant_id FROM tg_chats WHERE id = $1", chat_ref)
        if owner is not None and int(owner) != tenant_id:
            log.warning("попытка привязать чужой чат %s теннантом %s", chat_ref, tenant_id)
            return ("Этот чат обслуживается другой интеграцией.", "err")

    project_id, proj_name, client_name, err = await _ensure_project(
        tenant_id, b24_user_id, b24_group_id, client_id)
    if err:
        return (err, "err")

    async with pool().acquire() as conn:

        # Понятная проверка ДО обращения к триггеру: человеку нужно имя клиента,
        # а не текст исключения из базы.
        other = await conn.fetchrow(
            "SELECT c.name FROM chat_bindings b "
            "JOIN projects p ON p.id = b.project_id "
            "JOIN clients  c ON c.id = p.client_id "
            "WHERE b.chat_ref = $1 AND b.status = 'active' AND c.name <> $2 LIMIT 1",
            chat_ref, client_name)
        if other is not None:
            return (f"К этому чату уже привязаны проекты клиента "
                    f"<b>{esc_html(other['name'])}</b>. Один чат обслуживает одного "
                    f"клиента: выберите этого же клиента или отвяжите лишнее.", "err")

        await conn.execute(
            "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id, status) "
            "VALUES ($1,$2,$3,'active') "
            "ON CONFLICT (chat_ref, COALESCE(topic_ref, 0), project_id) "
            "DO UPDATE SET status = 'active'", tenant_id, chat_ref, project_id)

        await conn.execute(
            "UPDATE tg_chats SET tenant_id = $2, status = 'active', "
            "claimed_at = COALESCE(claimed_at, now()) WHERE id = $1", chat_ref, tenant_id)

    return (f"Чат привязан к проекту <b>{esc_html(proj_name)}</b> "
            f"(клиент {esc_html(client_name)}).", "ok")

async def _ensure_project(tenant_id: int, b24_user_id: int, b24_group_id: int,
                          client_id: int) -> tuple[int, str, str, str]:
    """Проект в нашей базе. Если его ещё нет — импортируем прямо сейчас.

    Название и признак экстранета берём с портала личным токеном пользователя:
    если у него нет доступа к группе, импорт не состоится, и это правильно.
    """
    from b24bot.b24 import errors
    from b24bot.domain import access

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT p.id, p.name, c.name AS client FROM projects p "
            "JOIN clients c ON c.id = p.client_id "
            "WHERE p.tenant_id = $1 AND p.b24_group_id = $2", tenant_id, b24_group_id)
    if row is not None:
        return int(row["id"]), row["name"], row["client"], ""

    try:
        client = await access.client_for_user(tenant_id, b24_user_id)
        async with client:
            groups = await client.call("sonet_group.get",
                                       {"FILTER": {"ID": b24_group_id}})
            stages = await client.call("task.stages.get", {"entityId": b24_group_id})
    except errors.B24Error as exc:
        return 0, "", "", f"Битрикс24 отказал: {exc.description or exc.code}"
    except Exception as exc:
        log.warning("импорт проекта %s не удался: %s", b24_group_id, str(exc)[:150])
        return 0, "", "", "Не удалось получить проект из Битрикс24."

    group = (groups or [None])[0]
    if not group:
        return 0, "", "", "Проект не найден на портале или недоступен вам."

    name = str(group.get("NAME") or f"Проект {b24_group_id}")

    async with pool().acquire() as conn, conn.transaction():
        if client_id:
            client_row = await conn.fetchrow(
                "SELECT id, name FROM clients WHERE id = $1 AND tenant_id = $2",
                client_id, tenant_id)
            if client_row is None:
                return 0, "", "", "Клиент не найден."
        else:
            client_row = await conn.fetchrow(
                "INSERT INTO clients (tenant_id, name) VALUES ($1,$2) "
                "ON CONFLICT (tenant_id, name) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id, name", tenant_id, name)

        pid = await conn.fetchval(
            """
            INSERT INTO projects (tenant_id, client_id, b24_group_id, name, is_extranet,
                                  owner_b24_user_id, name_synced_at)
            VALUES ($1,$2,$3,$4,$5,$6,now())
            ON CONFLICT (tenant_id, b24_group_id) DO UPDATE
              SET name = EXCLUDED.name, status = 'active', name_synced_at = now()
            RETURNING id
            """,
            tenant_id, client_row["id"], b24_group_id, name,
            group.get("IS_EXTRANET") == "Y",
            int(group.get("OWNER_ID") or 0) or None)

        for st in (stages or {}).values():
            await conn.execute(
                "INSERT INTO project_stages (tenant_id, project_id, b24_stage_id, title, "
                "sort, system_type, color) VALUES ($1,$2,$3,$4,$5,$6,$7) "
                "ON CONFLICT (tenant_id, project_id, b24_stage_id) DO UPDATE "
                "SET title = EXCLUDED.title, synced_at = now()",
                tenant_id, pid, int(st["ID"]), st["TITLE"], int(st.get("SORT") or 0),
                st.get("SYSTEM_TYPE"), st.get("COLOR"))

    log.info("импортирован проект %s «%s» для теннанта %s", b24_group_id, name, tenant_id)
    return int(pid), name, client_row["name"], ""



async def _connect_bot(tenant: asyncpg.Record, token: str) -> tuple[str, str]:
    if not tg.token_looks_valid(token):
        return ("Это не похоже на токен бота. Ожидается вид "
                "<code>123456789:AA...</code>", "err")

    try:
        me = await tg.get_me(token)
    except tg.TelegramInvalidToken:
        return ("Telegram не принял токен. Проверьте, что скопировали его целиком "
                "и что бот не был отозван в BotFather.", "err")
    except tg.TelegramError as exc:
        return (f"Telegram недоступен или ответил ошибкой: {esc_html(exc.description)}",
                "err")

    bot_id, username = int(me["id"]), str(me.get("username") or "")

    # Глобальная проверка: один бот не может обслуживать два теннанта.
    async with pool().acquire() as conn:
        owner = await conn.fetchval(
            "SELECT tenant_id FROM tg_bots WHERE bot_id = $1", bot_id)
    if owner is not None and owner != tenant["id"]:
        log.warning("ИНЦИДЕНТ: попытка привязать чужого бота %s к теннанту %s "
                    "(владелец %s)", bot_id, tenant["id"], owner)
        return ("Этот бот уже подключён к другой интеграции. Создайте отдельного бота "
                "в BotFather.", "err")

    settings = get_settings()
    secret = secrets.token_urlsafe(32)
    enc_token = box.encrypt(token, box.aad("tg_bots", "token", tenant["id"], bot_id))
    enc_secret = box.encrypt(secret, box.aad("tg_bots", "webhook_secret",
                                             tenant["id"], bot_id))

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tg_bots (tenant_id, bot_id, username, token, token_kid,
                                 webhook_secret, webhook_secret_kid, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,'pending')
            ON CONFLICT (tenant_id) DO UPDATE
              SET bot_id = EXCLUDED.bot_id, username = EXCLUDED.username,
                  token = EXCLUDED.token, token_kid = EXCLUDED.token_kid,
                  webhook_secret = EXCLUDED.webhook_secret,
                  webhook_secret_kid = EXCLUDED.webhook_secret_kid,
                  status = 'pending', last_error = NULL, updated_at = now()
            RETURNING webhook_id
            """,
            tenant["id"], bot_id, username, enc_token, box.kid_of(enc_token),
            enc_secret, box.kid_of(enc_secret))

    hook_url = f"{settings.public_base_url}/tg/{row['webhook_id']}"
    try:
        await tg.set_webhook(token, hook_url, secret)
    except tg.TelegramError as exc:
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE tg_bots SET status='error', last_error=$2 WHERE tenant_id=$1",
                tenant["id"], f"setWebhook: {exc.description}"[:500])
        return (f"Бот <b>@{esc_html(username)}</b> сохранён, но вебхук установить не "
                f"удалось: {esc_html(exc.description)}. Проверьте доступность "
                f"api.telegram.org с сервера.", "warn")

    privacy = await _probe_privacy(token)
    app_note = await _register_miniapp(token)
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE tg_bots SET status='active', privacy_mode_off=$2, last_check_at=now(), "
            "last_error=NULL WHERE tenant_id=$1", tenant["id"], privacy)

    tail = app_note + ("" if privacy is not False else
            " <b>Но privacy mode включён</b> — выполните <code>/setprivacy</code> → "
            "Disable в BotFather, иначе бот не увидит сообщения в группах.")
    return (f"Бот <b>@{esc_html(username)}</b> подключён, вебхук установлен.{tail}",
            "ok" if privacy is not False else "warn")


async def _recheck_bot(tenant: asyncpg.Record) -> tuple[str, str]:
    async with pool().acquire() as conn:
        bot = await conn.fetchrow(
            "SELECT bot_id, username, token, webhook_id, webhook_secret, mode "
            "FROM tg_bots WHERE tenant_id = $1", tenant["id"])
    if bot is None:
        return ("Бот не подключён.", "warn")

    token = box.decrypt(bot["token"],
                        box.aad("tg_bots", "token", tenant["id"], bot["bot_id"]))
    try:
        info = await tg.get_webhook_info(token)
    except tg.TelegramError as exc:
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE tg_bots SET status='error', last_error=$2, last_check_at=now() "
                "WHERE tenant_id=$1", tenant["id"], exc.description[:500])
        return (f"Telegram ответил ошибкой: {esc_html(exc.description)}", "err")

    expected = f"{get_settings().public_base_url}/tg/{bot['webhook_id']}"
    actual = str(info.get("url") or "")
    pending = int(info.get("pending_update_count") or 0)

    # В режиме polling пустой URL вебхука — НОРМА, а не сбой: поллер снимает вебхук
    # сам, потому что getUpdates и вебхук взаимоисключающи. Раньше эта проверка была
    # написана в предположении вебхука и приостанавливала исправно работающего бота.
    foreign = bool(actual) and actual != expected
    if bot["mode"] == "polling":
        if foreign:
            await _suspend(tenant["id"], bot["bot_id"], "вебхук указывает на чужой адрес")
            return ("У бота установлен посторонний вебхук, хотя мы работаем на long "
                    "polling. Похоже, токен попал к третьим лицам — перевыпустите его "
                    "в BotFather.", "err")
        if actual:
            # Наш же вебхук, оставшийся от прошлой конфигурации: мешает getUpdates.
            await tg.delete_webhook(token)
    elif actual != expected:
        await _suspend(tenant["id"], bot["bot_id"], "вебхук указывает на чужой адрес")
        return ("Вебхук бота указывает на посторонний адрес. Возможно, токен попал "
                "к третьим лицам — перевыпустите его в BotFather.", "err")

    privacy = await _probe_privacy(token)
    app_note = await _register_miniapp(token)
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE tg_bots SET status='active', privacy_mode_off=$2, last_check_at=now(), "
            "last_error=NULL WHERE tenant_id=$1", tenant["id"], privacy)

    last_err = info.get("last_error_message")
    extra = f" Последняя ошибка доставки: {esc_html(last_err)}." if last_err else ""
    return (f"Подключение в порядке. Необработанных обновлений: {pending}."
            f"{extra}{app_note}", "ok")


async def _register_miniapp(token: str) -> str:
    """Повесить мини-апп на кнопку меню бота. Делается за теннанта, а не им.

    Единственный шаг регистрации, доступный через Bot API: короткое имя приложения
    (`/newapp`) заводится только руками в BotFather и нужно лишь для открытия
    в одно касание из группы. Без него всё работает — на касание длиннее.
    """
    url = miniapp.web_app_url()
    if url is None:
        return ""
    try:
        await tg.set_chat_menu_button(token, url)
    except tg.TelegramError as exc:
        log.warning("кнопка меню не установлена: %s", exc)
        return (" Кнопку приложения в меню бота поставить не удалось — "
                "проверьте связь с Telegram.")
    return " Приложение подключено к кнопке меню бота."


async def _suspend(tenant_id: int, bot_id: int, reason: str) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE tg_bots SET status='suspended', last_check_at=now(), last_error=$2 "
            "WHERE tenant_id=$1", tenant_id, reason)
    log.error("ИНЦИДЕНТ: бот %s приостановлен: %s", bot_id, reason)


async def _probe_privacy(token: str) -> bool | None:
    """Косвенная проверка privacy mode.

    Прямого метода в Bot API нет. getMe у ботов с выключенным privacy отдаёт
    can_read_all_group_messages = true. Если поля нет — не знаем, и врать не будем.
    """
    try:
        me: dict[str, Any] = await tg.get_me(token)
    except tg.TelegramError:
        return None
    value = me.get("can_read_all_group_messages")
    return bool(value) if isinstance(value, bool) else None
