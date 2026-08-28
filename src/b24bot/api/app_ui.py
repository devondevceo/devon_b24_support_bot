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

from b24bot.api import ui_kit as ui
from b24bot.core.config import get_settings, is_trusted_portal_domain
from b24bot.core.text import esc_attr, esc_html
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.domain import access, audit, context, miniapp
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
TABS = ("overview", "chats", "bot", "survey", "approval", "notify", "team")


def safe_tab(value: str) -> str:
    """Активная вкладка возвращается с клиента, поэтому сверяется со списком."""
    return value if value in TABS else "overview"


def page(body: str, portal_domain: str | None, *, title: str = "Поддержка в Telegram",
         embedded: bool = True) -> HTMLResponse:
    """Экран приложения. `embedded=False` — та же вёрстка, но не внутри портала.

    Страница возврата из OAuth открывается верхнеуровнево в браузере, и грузить
    туда `BX24`-скрипт незачем: ни `fitWindow`, ни `installFinish` там некому
    вызывать, а внешний запрос из страницы, которая ничего не встраивает, — это
    лишний повод объясняться в модели угроз.
    """
    bx = '<script src="//api.bitrix24.com/api/v1/"></script>' if embedded else ""
    html = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{esc_html(title)}</title>"
            f"{bx}"
            f"<style>{ui.CSS}</style></head>"
            f'<body><div class="shell">{body}</div>'
            f"<script>{ui.SCRIPT}</script></body></html>")
    resp = HTMLResponse(html)
    # frame-ancestors — динамически, только под домен проверенного портала.
    # Общий *.bitrix24.ru позволил бы встроить страницу любому чужому порталу.
    if portal_domain and is_trusted_portal_domain(portal_domain):
        resp.headers["Content-Security-Policy"] = f"frame-ancestors https://{portal_domain}"
    else:
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return resp


def expired_page() -> HTMLResponse:
    """Истёкшая сессия — тоже экран, а не голый текст на белом фоне."""
    return page(
        _head_html("Поддержка в Telegram", "")
        + ui.panel("", ui.empty(
            "Сессия истекла",
            "Страница была открыта дольше получаса. Закройте приложение и откройте "
            "его заново из меню Битрикс24 — все настройки на месте.",
            icon_name="refresh")),
        None)


def _head_html(title: str, sub_html: str) -> str:
    """Шапка: кто мы, какой портал, кто вы. Одинаковая на всех экранах."""
    sub = f'<div class="head-sub">{sub_html}</div>' if sub_html else ""
    return (f'<header class="head"><div class="head-id">'
            f'<div class="mark">{ui.icon("send", 19)}</div>'
            f'<div class="head-t"><h1>{esc_html(title)}</h1>{sub}</div>'
            f"</div></header>")


def _health(bot: asyncpg.Record | None, chats: int, bindings: int) -> tuple[str, str, str]:
    """Одна строка о том, работает ли интеграция.

    Раньше это приходилось собирать в голове из шести строк «свойство — значение»
    на трёх разных карточках. Между тем ответ почти всегда определяется одним
    фактом, и порядок проверок здесь — это порядок, в котором всё ломается.

    Включённый privacy mode стоит рядом с отказом Telegram намеренно: бот при нём
    формально «работает», но не видит сообщений в группах, то есть продукта нет.
    """
    if bot is None:
        return ("warn", "Бот не подключён",
                "Пока не введён токен Telegram-бота, из чатов не работает ничего.")
    if bot["status"] == "suspended":
        return ("err", "Бот приостановлен",
                bot["last_error"] or "Подключение остановлено из соображений безопасности.")
    if bot["status"] == "error":
        return ("err", "Ошибка подключения",
                bot["last_error"] or "Telegram вернул ошибку при последней проверке.")
    if bot["privacy_mode_off"] is False:
        return ("err", "Включён privacy mode",
                "Бот видит в группах только команды и упоминания. Создать задачу "
                "ответом на сообщение коллеги нельзя, пока это не выключено.")
    if bot["status"] == "pending":
        return ("warn", "Бот подключается", "Подключение ещё не подтверждено.")
    if chats == 0:
        return ("warn", "Бот не добавлен ни в один чат",
                "Добавьте бота в групповой чат Telegram — чат появится здесь сам.")
    if bindings == 0:
        return ("warn", "Ни один чат не привязан к проекту",
                "Бот в чатах есть, но не знает, в какой проект складывать задачи.")
    if bot["privacy_mode_off"] is None:
        return ("warn", "Подключение давно не проверялось",
                "Не удалось подтвердить настройки бота при последней проверке.")
    return ("ok", "Интеграция работает",
            "Бот на связи, чаты привязаны к проектам. Задачи создаются из чатов.")


def _tabs_html(active: str, items: list[tuple[str, str, str, int]]) -> str:
    """Вкладки с счётчиками.

    Счётчик скрыт от скринридера и продублирован в `aria-label`: иначе имя
    кнопки склеивается в «Чаты3», и это же произносится вслух.
    """
    out = []
    for key, label, icon_name, count in items:
        sel = "true" if key == active else "false"
        cnt = (f'<span class="count" aria-hidden="true">{count}</span>'
               if count else "")
        name = f"{label}, {count}" if count else label
        out.append(
            f'<button type="button" class="tab" role="tab" data-tab="{esc_attr(key)}" '
            f'id="tab-{esc_attr(key)}" aria-controls="panel-{esc_attr(key)}" '
            f'aria-selected="{sel}" tabindex="{"0" if key == active else "-1"}" '
            f'aria-label="{esc_attr(name)}">'
            f'{ui.icon(icon_name, 15)}<span>{esc_html(label)}</span>{cnt}</button>')
    return f'<div class="tabs" role="tablist" aria-label="Разделы">{"".join(out)}</div>'


def _panel_html(key: str, active: str, body_html: str) -> str:
    hidden = "" if key == active else " hidden"
    return (f'<div role="tabpanel" data-panel="{esc_attr(key)}" id="panel-{esc_attr(key)}" '
            f'aria-labelledby="tab-{esc_attr(key)}"{hidden}>{body_html}</div>')


async def render_home(tenant: asyncpg.Record, b24_user_id: int, is_admin: bool,
                      session: str, *, message: str = "", message_kind: str = "ok",
                      active_tab: str = "overview") -> str:
    """Главный экран приложения.

    Два разных экрана, а не один с выключенными кнопками. Сотруднику нужен ровно
    один ответ — привязан ли его Telegram и в каких чатах он может работать;
    остальное для него шум, который раньше занимал четыре карточки из пяти.
    Управляющему нужен пульт, и он получает вкладки вместо простыни.
    """
    active = safe_tab(active_tab)

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
            "        AND status = 'active') AS bindings, "
            "       (SELECT count(*) FROM tg_chats c LEFT JOIN tg_bots b "
            "         ON b.id = c.bot_ref WHERE c.status <> 'migrated' "
            "         AND (c.tenant_id = $1 OR (c.tenant_id IS NULL "
            "              AND b.tenant_id = $1))) AS chats, "
            "       (SELECT count(*) FROM tenant_members WHERE tenant_id = $1) AS members",
            tenant["id"])

    my_role = await access.role_of_b24_user(int(tenant["id"]), b24_user_id)
    is_manager = is_admin or my_role == access.TENANT_ADMIN
    linked = await _link_state(int(tenant["id"]), b24_user_id)

    flash = ui.banner(message, message_kind) if message else ""

    if not is_manager:
        return await _employee_screen(tenant, b24_user_id, bot, linked, flash)

    return await _manager_screen(tenant, b24_user_id, is_admin, session, bot, counts,
                                 linked, active, flash)


# ------------------------------------------------------------------- сотрудник
async def _employee_screen(tenant: asyncpg.Record, b24_user_id: int,
                           bot: asyncpg.Record | None, linked: asyncpg.Record | None,
                           flash: str) -> str:
    """Экран обычного сотрудника: одно действие и ответ на один вопрос."""
    head = _head_html("Поддержка в Telegram",
                      f'{ui.icon("shield", 13)}<span>{esc_html(tenant["b24_domain"])}</span>'
                      f'<span>·</span><span>вы — сотрудник</span>')

    rows = await _my_projects(int(tenant["id"]))
    if rows:
        projects = f'<ul class="list">{"".join(rows)}</ul>'
        projects_panel = ui.panel("Ваши проекты в Telegram", projects,
                                  icon_name="folder", flush=True)
    else:
        projects_panel = ui.panel(
            "Ваши проекты в Telegram",
            ui.empty("Пока ни один чат не привязан",
                     "Как только администратор привяжет чат к проекту, он появится "
                     "здесь, и из него можно будет создавать задачи.",
                     icon_name="folder"),
            icon_name="folder")

    link = await _link_panel(tenant, b24_user_id, bot, linked)
    return head + flash + link + projects_panel


async def _my_projects(tenant_id: int) -> list[str]:
    """Проекты и чаты теннанта — то, что человек увидит в Telegram."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.name AS project, c.name AS client, ch.title, ch.chat_id
              FROM chat_bindings b
              JOIN projects p ON p.id = b.project_id
              JOIN clients  c ON c.id = p.client_id
              JOIN tg_chats ch ON ch.id = b.chat_ref
             WHERE b.tenant_id = $1 AND b.status = 'active'
             ORDER BY p.name
            """, tenant_id)
    out = []
    for r in rows:
        chat = r["title"] or f"чат {r['chat_id']}"
        out.append(ui.item(
            esc_html(r["project"]),
            sub_html=f'{ui.icon("chat", 12)} {esc_html(chat)}'
                     f' · клиент {esc_html(r["client"])}'))
    return out


# ------------------------------------------------------------------ управление
async def _manager_screen(tenant: asyncpg.Record, b24_user_id: int, is_admin: bool,
                          session: str, bot: asyncpg.Record | None,
                          counts: asyncpg.Record, linked: asyncpg.Record | None,
                          active: str, flash: str) -> str:
    rights = "администратор портала" if is_admin else "администратор теннанта"
    head = _head_html("Поддержка в Telegram",
                      f'{ui.icon("shield", 13)}<span>{esc_html(tenant["b24_domain"])}</span>'
                      f'<span>·</span><span>{esc_html(rights)}</span>')

    chats_n, bindings_n = int(counts["chats"]), int(counts["bindings"])
    kind, title, detail = _health(bot, chats_n, bindings_n)

    # Не привязанный к Telegram админ не может ничего сделать в самих чатах —
    # права у него есть, а инструмента нет. Это стоит отдельного предупреждения.
    warn_link = ""
    if not (linked and linked["link_status"] == "authorized"):
        warn_link = ui.banner(
            "<b>Ваш Telegram не привязан.</b> Права администратора у вас есть, но "
            "создавать и комментировать задачи из чатов вы пока не можете — "
            "привязка находится во вкладке «Команда».", "warn")

    from b24bot.api import app_approval, app_notify

    can_roles = await can_manage_admins(int(tenant["id"]), b24_user_id, is_admin)
    survey = await _survey_block(int(tenant["id"]), can_roles, session, active)
    approval_tab = await app_approval.render_block(int(tenant["id"]), b24_user_id,
                                                    can_roles, session, active)
    notify_tab = await app_notify.render_block(int(tenant["id"]), can_roles, session,
                                               active)

    tabs = _tabs_html(active, [
        ("overview", "Обзор", "info", 0),
        ("chats", "Чаты", "chat", chats_n),
        ("bot", "Бот", "send", 0),
        ("survey", "Опросник", "inbox", 0),
        ("approval", "Подтверждение", "check-circle", 0),
        ("notify", "Уведомления", "alert", 0),
        ("team", "Команда", "users", int(counts["members"])),
    ])

    overview = await _overview_panel(tenant, counts, bot, kind, title, detail,
                                     is_admin, session, active)
    chats = await _chats_block(tenant, b24_user_id, is_admin, session, active)
    # Мини-апп — это витрина того же бота, поэтому живёт на его вкладке,
    # а не отдельной: настраивать там нечего, кнопка меню ставится сама.
    bot_panel = (_bot_panel(bot, is_admin, session, active)
                 + _miniapp_block(bot, is_admin, session, active))
    team = await _team_panel(tenant, b24_user_id, is_admin, session, bot, linked, active)

    body = (_panel_html("overview", active, (flash if active == "overview" else "") + overview)
            + _panel_html("chats", active, (flash if active == "chats" else "") + chats)
            + _panel_html("bot", active, (flash if active == "bot" else "") + bot_panel)
            + _panel_html("survey", active, (flash if active == "survey" else "") + survey)
            + _panel_html("approval", active,
                         (flash if active == "approval" else "") + approval_tab)
            + _panel_html("notify", active,
                         (flash if active == "notify" else "") + notify_tab)
            + _panel_html("team", active, (flash if active == "team" else "") + team))

    return head + warn_link + tabs + body


async def _overview_panel(tenant: asyncpg.Record, counts: asyncpg.Record,
                          bot: asyncpg.Record | None, kind: str, title: str,
                          detail: str, is_admin: bool, session: str,
                          active: str) -> str:
    """Обзор — либо мастер настройки, либо состояние работающей интеграции.

    Пока настройка не доведена до конца, счётчики бессмысленны: везде нули.
    Поэтому до первой привязки здесь стоит список шагов, а не плитки.
    """
    chats_n, bindings_n = int(counts["chats"]), int(counts["bindings"])
    done = bot is not None and bot["status"] in ("active", "pending")
    complete = done and chats_n > 0 and bindings_n > 0

    health = (f'<div class="row-wrap" style="align-items:flex-start">'
              f'{ui.badge(title, kind)}</div>'
              f'<p class="hint" style="margin-top:10px">{esc_html(detail)}</p>')

    if not complete:
        # У активного шага есть кнопка, ведущая ровно туда, где он выполняется.
        # Второй шаг делается в самом Telegram, поэтому ведёт к боту, а не к вкладке.
        open_bot = ""
        if bot is not None and bot["username"]:
            open_bot = ui.link_button(f"https://t.me/{bot['username']}",
                                      "Открыть бота в Telegram", variant="sec",
                                      icon_name="external")
        steps = "".join([
            ui.step(1, "Подключить Telegram-бота",
                    "Создайте бота в @BotFather, выключите ему privacy mode и введите "
                    "токен во вкладке «Бот».",
                    state="done" if done else "now",
                    action_html=ui.goto_button("bot", "Перейти к настройке бота",
                                               icon_name="send")),
            ui.step(2, "Добавить бота в рабочий чат",
                    "Добавьте бота в групповой чат с клиентом. Чат появится во вкладке "
                    "«Чаты» сам — вводить его идентификатор вручную нельзя.",
                    state="done" if chats_n else ("now" if done else "todo"),
                    action_html=open_bot),
            ui.step(3, "Привязать чат к проекту",
                    "Свяжите чат с рабочей группой Битрикс24 — туда будут попадать "
                    "задачи из этого чата.",
                    state="done" if bindings_n else
                          ("now" if done and chats_n else "todo"),
                    action_html=ui.goto_button("chats", "Перейти к чатам",
                                               icon_name="chat")),
        ])
        return (ui.panel("Состояние", health, icon_name="info")
                + ui.panel("Что осталось настроить", f'<ol class="steps">{steps}</ol>',
                           icon_name="check-circle", flush=True))

    tiles = ui.stats([
        (str(chats_n), "чатов с ботом"),
        (str(bindings_n), "привязок к проектам"),
        (str(counts["projects"]), "проектов"),
        (str(counts["clients"]), "клиентов"),
    ])
    portal = (ui.field("Портал", f"<code>{esc_html(tenant['b24_domain'])}</code>")
              + ui.field("Установка приложения",
                         ui.badge("завершена", "ok")
                         if tenant["install_state"] == "finished"
                         else ui.badge(str(tenant["install_state"]), "warn"))
              + ui.field("Прав выдано порталом",
                         f'<span class="tnum">{len(tenant["granted_scope"] or [])}</span>'))
    return (ui.panel("Состояние", health, icon_name="info")
            + ui.panel("Охват", tiles, icon_name="folder")
            + ui.panel("Портал", portal, icon_name="shield"))


def _miniapp_block(bot: asyncpg.Record | None, is_admin: bool, session: str,
                   active: str = "bot") -> str:
    """Состояние мини-аппа. Настраивать здесь почти нечего — и это осознанно.

    Кнопка меню бота ставится сама при подключении и при проверке. Единственное
    ручное действие во всей цепочке — `/newapp` в BotFather, и оно НЕ обязательно:
    без него из группы открывается на касание длиннее, через личку бота.
    """
    if bot is None:
        return ""
    url = miniapp.web_app_url()
    if url is None:
        return ui.panel(
            "Приложение в Telegram",
            ui.empty("Приложение отключено",
                     "Публичный адрес сервиса не по https, а Telegram открывает "
                     "мини-апп только по https.", icon_name="alert"),
            icon_name="send")

    short = str(bot["miniapp_short_name"] or "")
    rows = (
        ui.field("Адрес", f"<code>{esc_html(url)}</code>")
        + ui.field("Кнопка меню бота", ui.badge("ставится автоматически", "ok"))
        + ui.field("Открытие из группы",
                   ui.badge("в одно касание", "ok") if short
                   else ui.badge("через личку бота", "warn"))
    )

    if not is_admin:
        return ui.panel("Приложение в Telegram", rows, icon_name="send")

    form = (
        f'<form method="post" action="/b24/app/miniapp">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        f'<input type="hidden" name="tab" value="{esc_attr(active)}">'
        f'<div class="f-group">'
        f'<label class="f-l" for="mini-short">Короткое имя приложения '
        f"из BotFather</label>"
        f'<input class="input mono" type="text" id="mini-short" name="short_name" '
        f'value="{esc_attr(short)}" placeholder="например tasks" '
        f'autocomplete="off" spellcheck="false" aria-describedby="mini-short-h">'
        f'<p class="hint" id="mini-short-h">Необязательно. Если в BotFather '
        f"выполнить <code>/newapp</code> и указать адрес выше, кнопка в групповом "
        f"чате откроет приложение сразу. Без этого она сначала ведёт в личку "
        f"бота — работает так же, просто на касание больше.</p></div>"
        f'<div class="btn-row"><button class="btn sec" type="submit">'
        f'{ui.icon("check", 15)}Сохранить имя</button></div></form>')
    return ui.panel("Приложение в Telegram", rows + '<div class="divider"></div>' + form,
                    icon_name="send")


@router.post("/miniapp")
async def save_miniapp(session: str = Form(...), short_name: str = Form(""),
                       tab: str = Form("bot")) -> HTMLResponse:
    sess = await load_session(session)
    if sess is None:
        return expired_page()

    tab = safe_tab(tab)
    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants WHERE id = $1",
            sess["tenant_id"])

    if not sess["is_portal_admin"]:
        body = await render_home(tenant, sess["b24_user_id"], False, session,
                                 message="Настраивать приложение может только "
                                         "администратор портала.",
                                 message_kind="err", active_tab=tab)
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
                             message=message, message_kind=kind, active_tab=tab)
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
                       session: str, active: str = "chats") -> str:
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
        return ui.panel(
            "Чаты",
            ui.empty("Бот пока не добавлен ни в один чат",
                     "Добавьте бота в групповой чат Telegram — чат появится здесь сам. "
                     "Вводить идентификатор чата вручную нельзя: принадлежность "
                     "подтверждается фактом присутствия бота.",
                     icon_name="chat"),
            icon_name="chat")

    by_chat: dict[int, list[asyncpg.Record]] = {}
    for b in bindings:
        by_chat.setdefault(b["chat_ref"], []).append(b)

    sections = "".join(
        _chat_section(ch, by_chat.get(ch["id"], []), portal, known_ids, clients,
                      session, active, is_admin)
        for ch in chats)

    err = ui.banner(esc_html(portal_error), "warn") if portal_error else ""
    # Кнопки «добавить чат» нет намеренно (принадлежность подтверждается
    # присутствием бота), но человек, ищущий её, обязан узнать это здесь,
    # а не сдаться после осмотра всех углов экрана.
    tail = ui.hint("Новый чат появляется здесь сам, как только Telegram-бота "
                   "добавят в группу.")
    if not is_admin:
        tail += ui.hint("Управлять привязками может администратор портала.")
    note = (f'<span class="panel-note tnum">чатов: {len(chats)} · '
            f"привязок: {len(bindings)}</span>")
    return err + ui.panel("Чаты и проекты", sections, icon_name="chat",
                          flush=True, actions_html=note, footer_html=tail)


def _chat_state(status: str, has_bindings: bool) -> tuple[str, str]:
    """Статус чата глазами пользователя, а не таблицы tg_chats.

    «Работает» у чата без единой привязки — уверенная неправда: бот в чате
    есть, а задачи создавать некуда. Прежняя вкладка показывала зелёный статус
    и серую строку «проектов пока нет» рядом — два противоположных ответа на
    один вопрос. Статус присутствия бота и статус пригодности к работе здесь
    сведены в один честный: без проекта чат не работает, каким бы живым ни
    был бот.
    """
    if status == "left":
        return "бот удалён из чата", "err"
    if not has_bindings:
        return "без проекта", "warn"
    if status == "claimed":
        return "подключён", "ok"
    if status == "active":
        return "работает", "ok"
    return status, "neutral"


def _chat_section(ch: asyncpg.Record, linked: list[asyncpg.Record],
                  portal: list[dict[str, Any]], known_ids: set[int],
                  clients: list[asyncpg.Record], session: str, active: str,
                  is_admin: bool) -> str:
    """Раздел одного чата: шапка с клиентом, строки проектов, форма привязки.

    Клиент назван один раз в шапке, а не на каждой строке проекта: один чат
    обслуживает ровно одного клиента (доменная модель), и повтор
    «Devon SD BOT · клиент Devon SD BOT» под каждым проектом читался как сбой
    вёрстки, а не как информация.

    У чата, из которого бота удалили, вместо формы привязки — что случилось и
    что сделать: прежний текст «проектов пока нет» рассказывал про проекты,
    когда проблема была в боте.
    """
    label, kind = _chat_state(str(ch["status"]), bool(linked))
    chat_name = str(ch["title"] or f"чат {ch['chat_id']}")

    sub_bits: list[str] = []
    if linked:
        sub_bits.append(f"<span>клиент {esc_html(linked[0]['client'])}</span>")
    sub_bits.append(f'<span class="grp-id tnum">{esc_html(ch["chat_id"])}</span>')
    if ch["is_forum"]:
        sub_bits.append("<span>форум</span>")
    sub = '<span aria-hidden="true">·</span>'.join(sub_bits)

    rows: list[str] = []
    for b in linked:
        btn = ""
        if is_admin:
            btn = ui.action_form(
                "/b24/app/chat",
                {"session": session, "action": "unbind", "binding_id": b["id"],
                 "tab": active},
                "Отвязать", variant="danger", icon_name="unlink",
                confirm=f"Отвязать проект «{b['project']}» от чата "
                        f"«{chat_name}»?\n\nЗадачи из этого чата больше не будут "
                        f"попадать в проект.")
        rows.append(ui.group_row(esc_html(str(b["project"])), actions_html=btn,
                                 icon_name="folder"))

    body = "".join(rows)
    if str(ch["status"]) == "left":
        body += ui.note(
            "Верните бота в группу в Telegram"
            + (" — привязки и настройки сохранились." if linked
               else ", затем привяжите проект."))
    elif not linked:
        body += ui.note("Чат не привязан к проекту — задачи из него пока "
                        "некуда создавать.")

    if is_admin and str(ch["status"]) != "left":
        form = _bind_form(ch, linked, portal, known_ids, clients, session, active)
        body += f'<div class="grp-p">{form}</div>'

    return ui.group(chat_name, sub_html=sub, actions_html=ui.badge(label, kind),
                    body_html=body, icon_name="chat")


def _bind_form(chat: asyncpg.Record, linked: list[asyncpg.Record],
               portal: list[dict[str, Any]], known_ids: set[int],
               clients: list[asyncpg.Record], session: str, active: str) -> str:
    """Привязка чата к проекту портала.

    Свёрнута за `<details>`: постоянно раскрытая на каждом чате, форма занимала
    больше места, чем сами чаты, и вкладка читалась как простыня из селектов.
    У чата без привязок форма раскрыта сразу — привязка и есть следующий шаг.

    Клиент выбирается только у чата БЕЗ привязок. Дальше он фиксирован: один
    чат обслуживает одного клиента, и селект предлагал бы выбор между
    «правильно» и «ошибка сервера». Явность выбора при этом сохранена — просто
    выбор делается один раз, первой привязкой.
    """
    bound = {int(b["b24_group_id"]) for b in linked if b["b24_group_id"]}
    available = [g for g in portal if g["id"] not in bound]
    if not available:
        return ui.hint("Все доступные вам проекты портала уже привязаны "
                       "к этому чату.")

    options = "".join(
        f'<option value="{esc_attr(g["id"])}">{esc_html(g["name"])}'
        f'{"" if g["id"] in known_ids else " — новый"}</option>'
        for g in available)
    cid = esc_attr(chat["id"])

    project_field = (
        f'<div class="f-group">'
        f'<label class="f-l" for="proj-{cid}">Проект портала</label>'
        f'<select class="input" id="proj-{cid}" name="b24_group_id">{options}</select>'
        f"</div>")

    if linked:
        fixed = linked[0]
        client_ctl = (f'<input type="hidden" name="client_id" '
                      f'value="{esc_attr(fixed["client_id"])}">')
        fields = project_field
        note = ui.hint(f"Проект привяжется к клиенту «{fixed['client']}»: он у "
                       f"этого чата уже есть, а второго быть не может.")
    else:
        client_opts = "".join(
            f'<option value="{esc_attr(c["id"])}">{esc_html(c["name"])}</option>'
            for c in clients)
        client_ctl = ""
        fields = (
            f'<div class="grid2">{project_field}'
            f'<div class="f-group">'
            f'<label class="f-l" for="cl-{cid}">Клиент</label>'
            f'<select class="input" id="cl-{cid}" name="client_id">'
            f'<option value="0">Создать по названию проекта</option>{client_opts}</select>'
            f"</div></div>")
        note = ui.hint("Один чат обслуживает одного клиента: все проекты этого "
                       "чата должны принадлежать ему.")

    return (
        f'<details class="bind"{"" if linked else " open"}>'
        f'<summary>{ui.icon("plus", 15)}Привязать проект</summary>'
        f'<form method="post" action="/b24/app/chat">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        f'<input type="hidden" name="action" value="bind">'
        f'<input type="hidden" name="tab" value="{esc_attr(active)}">'
        f'<input type="hidden" name="chat_ref" value="{cid}">'
        f"{client_ctl}{fields}"
        f'<div class="btn-row" style="margin-top:4px">'
        f'<button class="btn" type="submit">Привязать</button></div>'
        f"</form>{note}</details>")


async def _link_state(tenant_id: int, b24_user_id: int) -> asyncpg.Record | None:
    async with pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT u.tg_username, m.link_status FROM tenant_members m "
            "JOIN users u ON u.id = m.user_id "
            "WHERE m.tenant_id = $1 AND m.b24_user_id = $2", tenant_id, b24_user_id)


async def _link_panel(tenant: asyncpg.Record, b24_user_id: int,
                      bot: asyncpg.Record | None,
                      linked: asyncpg.Record | None) -> str:
    """Привязка Telegram — ОСНОВНОЙ путь сопоставления.

    Телефон заполнен у 5 сотрудников из 27, поэтому сопоставление по нему как главный
    механизм нежизнеспособно. Здесь мы уже знаем, кто человек: портал сам прислал его
    токен. Остаётся связать это с его telegram-аккаунтом одноразовой ссылкой.

    Для сотрудника это единственное действие на экране, поэтому кнопка крупная и
    занимает всю ширину, а не прячется последней строкой в последней карточке.
    """
    if bot is None:
        return ui.panel(
            "Ваш Telegram",
            ui.empty("Привязка пока недоступна",
                     "Она откроется, когда администратор портала подключит "
                     "Telegram-бота теннанта.", icon_name="send"),
            icon_name="user")

    if linked and linked["link_status"] == "authorized":
        who = (f"@{esc_html(linked['tg_username'])}" if linked["tg_username"]
               else "аккаунт привязан")
        body = (ui.field("Telegram", f'<span class="row-wrap">{who}'
                                     f'{ui.badge("привязан", "ok")}</span>')
                + ui.field("Пользователь Битрикс24",
                           f'<code class="tnum">{esc_html(b24_user_id)}</code>')
                + ui.hint("Вы можете создавать и комментировать задачи из привязанных "
                          "чатов — в пределах прав, которые вам даёт сам Битрикс24."))
        return ui.panel("Ваш Telegram", body, icon_name="user")

    # Токен одноразовый и живёт 15 минут, поэтому выпускается при показе экрана.
    token = await context.issue_token(
        "link", tenant_id=tenant["id"], payload={"b24_user_id": b24_user_id},
        ttl=timedelta(minutes=15))
    url = f"https://t.me/{bot['username']}?start=b{token}"
    note = ui.hint("Ссылка одноразовая и действует 15 минут. Если открываете с "
                   "компьютера, а Telegram у вас на телефоне — перешлите себе "
                   "адрес ниже.")

    body = (
        f'<div class="row-wrap" style="margin-bottom:14px">'
        f'{ui.badge("не привязан", "warn")}</div>'
        f"<p>Нажмите кнопку — откроется чат с ботом "
        f"<code>@{esc_html(bot['username'])}</code>. В нём нажмите «Запустить», "
        f"и аккаунты свяжутся.</p>"
        f'<div class="btn-row">'
        f'{ui.link_button(url, "Привязать Telegram", icon_name="link")}</div>'
        f'{note}<p class="hint"><code>{esc_html(url)}</code></p>')
    return ui.panel("Ваш Telegram", body, icon_name="user")


def _bot_panel(bot: asyncpg.Record | None, is_admin: bool, session: str,
               active: str) -> str:
    """Вкладка «Бот»: подключение и состояние.

    Поле токена получило настоящую подпись вместо плейсхолдера: плейсхолдер
    исчезает при вводе, и человек перестаёт видеть, что именно он заполняет.
    """
    if bot is None:
        if not is_admin:
            return ui.panel(
                "Telegram-бот",
                ui.empty("Бот не подключён",
                         "Подключить бота может администратор портала Битрикс24. "
                         "Обратитесь к нему — без бота интеграция не работает.",
                         icon_name="send"),
                icon_name="send")
        form = (
            f'<form method="post" action="/b24/app/bot">'
            f'<input type="hidden" name="session" value="{esc_attr(session)}">'
            f'<input type="hidden" name="action" value="save">'
            f'<input type="hidden" name="tab" value="{esc_attr(active)}">'
            f'<div class="f-group">'
            f'<label class="f-l" for="bot-token">Токен бота из @BotFather</label>'
            f'<input class="input mono" type="text" id="bot-token" name="token" '
            f'placeholder="123456789:AA..." autocomplete="off" spellcheck="false" '
            f'aria-describedby="bot-token-h">'
            f'<p class="hint" id="bot-token-h">Токен виден только при вводе. '
            f'Обратно он не показывается никогда и хранится в шифрованном виде.</p>'
            f"</div>"
            f'<button class="btn" type="submit">{ui.icon("link", 15)}'
            f"Подключить бота</button></form>")
        steps = (
            "<p>Создайте бота командой <code>/newbot</code> в "
            "<code>@BotFather</code>, затем обязательно выполните там же "
            "<code>/setprivacy</code> → выберите бота → <b>Disable</b>.</p>"
            "<p class=\"hint\">Без этого бот видит в группе только команды и "
            "упоминания, и создать задачу ответом на сообщение коллеги будет "
            "нельзя — это самая частая причина «бот не работает».</p>")
        return ui.panel("Подключение бота", steps + '<div class="divider"></div>' + form,
                        icon_name="send")

    privacy = bot["privacy_mode_off"]
    privacy_html = {
        True: ui.badge("выключен, как надо", "ok"),
        False: ui.badge("включён — бот не видит сообщений", "err"),
        None: ui.badge("не проверено", "warn"),
    }[privacy]
    status_html = {
        "active": ui.badge("работает", "ok"),
        "pending": ui.badge("подключается", "warn"),
        "error": ui.badge("ошибка", "err"),
        "suspended": ui.badge("приостановлен", "err"),
    }.get(bot["status"], ui.badge(str(bot["status"]), "neutral"))

    rows = (ui.field("Бот", f"<code>@{esc_html(bot['username'])}</code>")
            + ui.field("Состояние", status_html)
            + ui.field("Режим приёма", esc_html(
                "long polling через прокси" if bot["mode"] == "polling" else "вебхук"))
            + ui.field("Privacy mode", privacy_html)
            + ui.field("Последняя проверка", esc_html(
                bot["last_check_at"].strftime("%d.%m.%Y %H:%M")
                if bot["last_check_at"] else "—")))

    err = ""
    if bot["last_error"]:
        err = ui.banner(f"<b>Последняя ошибка.</b> {esc_html(bot['last_error'])}", "err")

    fix = ""
    if privacy is False:
        fix = ui.banner(
            "<b>Выключите privacy mode.</b> В <code>@BotFather</code>: "
            "<code>/setprivacy</code> → выберите бота → <b>Disable</b>. "
            "Затем нажмите «Проверить подключение».", "warn")

    actions = ""
    if is_admin:
        actions = ui.action_form(
            "/b24/app/bot",
            {"session": session, "action": "recheck", "tab": active},
            "Проверить подключение", variant="sec", icon_name="refresh")

    replace = ""
    if is_admin:
        replace = (
            f'<details class="bind">'
            f'<summary>{ui.icon("refresh", 15)}Заменить бота другим</summary>'
            f'<form method="post" action="/b24/app/bot" style="margin-top:12px">'
            f'<input type="hidden" name="session" value="{esc_attr(session)}">'
            f'<input type="hidden" name="action" value="save">'
            f'<input type="hidden" name="tab" value="{esc_attr(active)}">'
            f'<div class="f-group">'
            f'<label class="f-l" for="bot-token2">Новый токен из @BotFather</label>'
            f'<input class="input mono" type="text" id="bot-token2" name="token" '
            f'placeholder="123456789:AA..." autocomplete="off" spellcheck="false">'
            f"</div>"
            f'<button class="btn sec" type="submit" data-confirm="Заменить '
            f'подключённого бота на другого? Чаты старого бота перестанут '
            f'обслуживаться.">Заменить бота</button></form></details>')

    return (err + fix
            + ui.panel("Telegram-бот", rows, icon_name="send", actions_html=actions,
                       footer_html=replace))


# ------------------------------------------------------------------ сохранение
@router.post("/bot")
async def save_bot(request: Request, session: str = Form(...), action: str = Form("save"),
                   token: str = Form(""), tab: str = Form("bot")) -> HTMLResponse:
    sess = await load_session(session)
    if sess is None:
        return expired_page()

    tab = safe_tab(tab)
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
                                         "портала.", message_kind="err", active_tab=tab)
        return page(body, domain)

    message, kind = "", "ok"

    if action == "save":
        message, kind = await _connect_bot(tenant, token.strip())
    elif action == "recheck":
        message, kind = await _recheck_bot(tenant)

    async with pool().acquire() as conn:
        fresh = await issue_session(conn, tenant["id"], sess["b24_user_id"], True)
    body = await render_home(tenant, sess["b24_user_id"], True, fresh,
                             message=message, message_kind=kind, active_tab=tab)
    return page(body, domain)


@router.post("/chat")
async def chat_action(session: str = Form(...), action: str = Form(...),
                      chat_ref: int = Form(0), b24_group_id: int = Form(0),
                      client_id: int = Form(0), binding_id: int = Form(0),
                      tab: str = Form("chats")) -> HTMLResponse:
    sess = await load_session(session)
    if sess is None:
        return expired_page()

    tab = safe_tab(tab)
    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants WHERE id = $1",
            sess["tenant_id"])

    if not sess["is_portal_admin"]:
        body = await render_home(tenant, sess["b24_user_id"], False, session,
                                 message="Управлять привязками может только администратор "
                                         "портала.", message_kind="err", active_tab=tab)
        return page(body, tenant["b24_domain"])

    message, kind = await _apply_chat_action(
        int(tenant["id"]), int(sess["b24_user_id"]), action, chat_ref,
        b24_group_id, client_id, binding_id)

    async with pool().acquire() as conn:
        fresh = await issue_session(conn, tenant["id"], sess["b24_user_id"], True)
    body = await render_home(tenant, sess["b24_user_id"], True, fresh,
                             message=message, message_kind=kind, active_tab=tab)
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
    from b24bot.domain import access, sync

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
            raw_stages = await client.call("task.stages.get", {"entityId": b24_group_id})
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

        await sync.apply_stages(conn, tenant_id, int(pid),
                                sync.parse_stages(raw_stages, b24_group_id))

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


# ------------------------------------------------------------- админы теннанта
async def can_manage_admins(tenant_id: int, b24_user_id: int,
                            is_portal_admin: bool) -> bool:
    """Кто распоряжается ролями.

    Администратор портала — всегда: он поставил приложение, и отнимать у него это
    право внутри нашего интерфейса было бы фикцией, он всё равно переустановит.
    Плюс это единственный способ выдать права первому админу теннанта, пока их нет
    ни у кого.
    """
    if is_portal_admin:
        return True
    return await access.role_of_b24_user(tenant_id, b24_user_id) == access.TENANT_ADMIN


async def _b24_names(tenant_id: int, b24_user_id: int, ids: list[int]) -> dict[int, str]:
    """Имена сотрудников портала одним batch-вызовом.

    Без них список выглядит как «Битрикс24 #7», и назначать по такому списку права
    страшно. Отказ портала здесь не критичен: подписи деградируют до номеров.
    """
    if not ids:
        return {}
    try:
        client = await access.client_for_user(tenant_id, b24_user_id)
        async with client:
            res = await client.batch({str(i): ("user.get", {"ID": i}) for i in ids[:50]})
    except Exception as exc:
        log.warning("не удалось получить имена сотрудников: %s", str(exc)[:150])
        return {}

    # batch() отдаёт свою обёртку: {"result": {...}, "errors": {...}, ...}.
    # Ошибки по отдельным ключам он уже залогировал, нам нужны только удачные.
    out: dict[int, str] = {}
    for key, value in ((res or {}).get("result") or {}).items():
        row = (value or [None])[0] if isinstance(value, list) else value
        if not isinstance(row, dict):
            continue
        name = " ".join(x for x in (row.get("NAME"), row.get("LAST_NAME")) if x).strip()
        out[int(key)] = name or str(row.get("EMAIL") or key)
    return out


async def _team_panel(tenant: asyncpg.Record, b24_user_id: int, is_admin: bool,
                      session: str, bot: asyncpg.Record | None,
                      linked: asyncpg.Record | None, active: str) -> str:
    """Вкладка «Команда»: люди и права, плюс собственная привязка.

    Своя привязка стоит первой намеренно: у администратора без неё права есть,
    а работать в чатах нечем, и это состояние надо видеть раньше чужих ролей.
    """
    can_roles = await can_manage_admins(int(tenant["id"]), b24_user_id, is_admin)
    mine = await _link_panel(tenant, b24_user_id, bot, linked)
    admins = await _admins_block(tenant, b24_user_id, can_roles, session, active)
    return mine + admins


async def _admins_block(tenant: asyncpg.Record, b24_user_id: int, can_manage: bool,
                        session: str, active: str = "team") -> str:
    async with pool().acquire() as conn:
        members = await conn.fetch(
            """
            SELECT m.user_id, m.role, m.b24_user_id, m.link_status,
                   u.tg_username, u.display_name
              FROM tenant_members m
              JOIN users u ON u.id = m.user_id
             WHERE m.tenant_id = $1
             ORDER BY (m.role = 'tenant_admin') DESC, u.display_name
            """, tenant["id"])

    if not members:
        return ui.panel(
            "Администраторы",
            ui.empty("Пока никто не привязал Telegram",
                     "Права назначаются тем, кто уже связал аккаунты: администратор — "
                     "это конкретный человек с личным токеном Битрикс24, а не строка "
                     "в таблице.", icon_name="users"),
            icon_name="users")

    names = await _b24_names(int(tenant["id"]), b24_user_id,
                             [int(m["b24_user_id"]) for m in members
                              if m["b24_user_id"] is not None])
    admins = sum(1 for m in members if m["role"] == "tenant_admin")

    # Ограничение выборки имён — не «деталь реализации», а видимый факт:
    # молча показать 50 из 60 человек значит соврать про состав команды.
    shown = ui.hint(f"Показаны все {len(members)} участников.") if len(members) <= 50 \
        else ui.hint(f"Имена подтянуты для первых 50 участников из {len(members)}; "
                     f"у остальных вместо имени показан номер в Битрикс24.")

    rows = []
    for m in members:
        row_is_admin = m["role"] == "tenant_admin"
        b24_id = int(m["b24_user_id"]) if m["b24_user_id"] is not None else None
        who = names.get(b24_id if b24_id is not None else -1) or m["display_name"] \
            or "без имени"
        tg = f"@{m['tg_username']}" if m["tg_username"] else "telegram не показан"
        b24_part = f" · Б24 #{b24_id}" if b24_id is not None else ""
        role_badge = (ui.badge("админ теннанта", "ok") if row_is_admin
                      else ui.badge("сотрудник", "neutral"))
        link_badge = ("" if m["link_status"] == "authorized"
                      else ui.badge(str(m["link_status"]), "warn"))

        btn = ""
        if can_manage:
            # Последнего админа снять нельзя: теннант остался бы без управления,
            # а вернуть его можно было бы только руками в базе.
            last_one = row_is_admin and admins <= 1
            btn = ui.action_form(
                "/b24/app/role",
                {"session": session,
                 "action": "revoke" if row_is_admin else "grant",
                 "member_user_id": m["user_id"], "tab": active},
                "Снять права" if row_is_admin else "Назначить админом",
                variant="danger" if row_is_admin else "ghost",
                icon_name="shield",
                disabled=last_one,
                title="Это единственный админ теннанта" if last_one else "",
                confirm=(f"Снять права администратора у {who}?" if row_is_admin
                         else f"Назначить {who} администратором теннанта? "
                              f"Он сможет привязывать чаты, менять бота и "
                              f"назначать других администраторов."))

        rows.append(ui.item(
            esc_html(who),
            sub_html=f"{esc_html(tg)}{esc_html(b24_part)}",
            actions_html=f"{link_badge}{role_badge}{btn}"))

    foot = (ui.hint("Админ теннанта привязывает чаты к проектам, вводит токен бота и "
                    "назначает других админов. Обычный сотрудник создаёт и "
                    "комментирует задачи — в пределах прав, которые ему дал Битрикс24.")
            if can_manage else
            ui.hint("Назначать администраторов может администратор портала или "
                    "действующий администратор теннанта."))

    return ui.panel("Администраторы", f'<ul class="list">{"".join(rows)}</ul>',
                    icon_name="users", flush=True, footer_html=shown + foot)


@router.post("/role")
async def role_action(session: str = Form(...), action: str = Form(...),
                      member_user_id: int = Form(0),
                      tab: str = Form("team")) -> HTMLResponse:
    sess = await load_session(session)
    if sess is None:
        return expired_page()

    tab = safe_tab(tab)
    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants WHERE id = $1",
            sess["tenant_id"])

    tenant_id, actor = int(tenant["id"]), int(sess["b24_user_id"])
    is_portal_admin = bool(sess["is_portal_admin"])

    # Право проверяется здесь, а не только при отрисовке кнопки: форму можно
    # отправить и без неё (docs/40-security.md §3).
    if not await can_manage_admins(tenant_id, actor, is_portal_admin):
        log.warning("попытка сменить роль без прав: теннант %s, пользователь %s",
                    tenant_id, actor)
        message, kind = "Назначать администраторов может только администратор.", "err"
    else:
        message, kind = await _apply_role(tenant_id, actor, action, member_user_id)

    async with pool().acquire() as conn:
        fresh = await issue_session(conn, tenant_id, actor, is_portal_admin)
    body = await render_home(tenant, actor, is_portal_admin, fresh,
                             message=message, message_kind=kind, active_tab=tab)
    return page(body, tenant["b24_domain"])


async def _apply_role(tenant_id: int, actor_b24_id: int, action: str,
                      member_user_id: int) -> tuple[str, str]:
    if action not in ("grant", "revoke"):
        return "Неизвестное действие.", "err"

    async with pool().acquire() as conn, conn.transaction():
        target = await conn.fetchrow(
            "SELECT m.role, m.b24_user_id, m.link_status, u.display_name, u.tg_username "
            "FROM tenant_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.tenant_id = $1 AND m.user_id = $2 FOR UPDATE",
            tenant_id, member_user_id)
        if target is None:
            return "Этот человек не состоит в теннанте.", "err"

        if action == "grant" and target["link_status"] != "authorized":
            return ("Сначала человек должен привязать Telegram к Битрикс24: без личного "
                    "токена права администратора ничего не дадут.", "err")

        new_role = access.TENANT_ADMIN if action == "grant" else access.MEMBER
        if target["role"] == new_role:
            return "Роль уже такая, ничего не меняли.", "ok"

        if action == "revoke":
            # Считаем под тем же локом: два одновременных снятия иначе оставят
            # теннант без единого администратора.
            admins = await conn.fetchval(
                "SELECT count(*) FROM tenant_members "
                "WHERE tenant_id = $1 AND role = 'tenant_admin'", tenant_id)
            if int(admins) <= 1:
                return ("Это единственный администратор теннанта. Сначала назначьте "
                        "другого — иначе управлять интеграцией станет некому.", "err")

        await conn.execute(
            "UPDATE tenant_members SET role = $3 WHERE tenant_id = $1 AND user_id = $2",
            tenant_id, member_user_id, new_role)

    who = target["display_name"] or (f"@{target['tg_username']}"
                                     if target["tg_username"] else str(member_user_id))
    await audit.record(
        tenant_id, "role.grant" if action == "grant" else "role.revoke",
        actor_id=actor_b24_id, target=f"member:{member_user_id}",
        detail={"кому": who, "было": target["role"], "стало": new_role,
                "b24_user_id": target["b24_user_id"]})
    log.info("роль в теннанте %s изменена: user_id=%s %s -> %s (кем: Б24 %s)",
             tenant_id, member_user_id, target["role"], new_role, actor_b24_id)

    return ((f"{esc_html(who)} — теперь администратор теннанта." if action == "grant"
             else f"С {esc_html(who)} сняты права администратора."), "ok")


# -------------------------------------------------------------------- опросник
async def _survey_block(tenant_id: int, can_manage: bool, session: str,
                        active: str) -> str:
    """Сводка по наборам вопросов плюс вход в конструктор.

    Сам конструктор — отдельная страница: там своя навигация по наборам и своя
    форма вопроса, во вкладку это не помещается. Здесь только состав наборов
    и кнопка входа.
    """
    from b24bot.api import app_survey

    templates = await app_survey.templates_of(tenant_id)
    own = sum(1 for t in templates if t["tenant_id"] is not None)

    if not templates:
        return ui.panel(
            "Опросник",
            ui.empty("Ни одного набора вопросов нет",
                     "Опросник — это то, что бот спросит в чате перед созданием "
                     "задачи. Без набора он создаст задачу из одного сообщения.",
                     icon_name="inbox"),
            icon_name="inbox")

    rows = [
        ui.item(esc_html(t["title"]),
                sub_html=f'вопросов: <span class="tnum">{esc_html(t["questions"])}</span>',
                actions_html=(ui.badge("свой", "ok") if t["tenant_id"] is not None
                              else ui.badge("системный", "neutral")))
        for t in templates]
    body = f'<ul class="list">{"".join(rows)}</ul>'

    if not can_manage:
        return ui.panel("Опросник", body, icon_name="inbox", flush=True,
                        footer_html=ui.hint("Настраивать опросник может "
                                            "администратор теннанта."))

    open_form = (
        '<form method="post" action="/b24/app/survey" class="inline">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        '<input type="hidden" name="action" value="open">'
        f'<input type="hidden" name="tab" value="{esc_attr(active)}">'
        f'<input type="hidden" name="template_id" value="{templates[0]["id"]}">'
        f'<button class="btn" type="submit">{ui.icon("settings", 15)}'
        "Настроить опросник</button></form>")

    tail = ui.hint(
        "Ответы на вопросы, связанные с полями задачи, уходят в эти поля. "
        "Остальные — в тело задачи. "
        + (f"Своих наборов: {own}." if own else
           "Пока все наборы системные: первое изменение скопирует набор вам."))

    return ui.panel("Опросник", body, icon_name="inbox", flush=True,
                    actions_html=open_form, footer_html=tail)


@router.post("/back")
async def back_to_home(session: str = Form(...),
                       tab: str = Form("survey")) -> HTMLResponse:
    """Возврат из конструктора опросника на главный экран.

    Отдельный маршрут, а не ссылка: сессия страницы живёт в теле POST — в
    GET-параметре она попадала бы в логи, историю браузера и Referer.
    """
    sess = await load_session(session)
    if sess is None:
        return expired_page()

    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants "
            "WHERE id = $1", sess["tenant_id"])
        fresh = await issue_session(conn, tenant["id"], sess["b24_user_id"],
                                    bool(sess["is_portal_admin"]))

    body = await render_home(tenant, sess["b24_user_id"],
                             bool(sess["is_portal_admin"]), fresh,
                             active_tab=safe_tab(tab))
    return page(body, tenant["b24_domain"])
