"""Настройка уведомлений — вкладка приложения Б24.

Экран отвечает на два вопроса и держит их раздельно: **о чём сообщать** (набор
событий) и **как часто** (сразу или сводкой раз в N минут). Обе ручки живут на
трёх уровнях — теннант, проект, конкретный чат, — и уровень ниже перекрывает
уровень выше (инвариант И-9, `domain/notifications.py`).

Почему уровней три, а не один. «Нас заваливает уведомлениями» — это всегда про
конкретный чат: в одном проекте у клиента дежурная смена и ей нужно всё, в
соседнем чате того же проекта сидит руководитель, которому хватает сводки раз в
час. Настройка только на уровне проекта заставила бы выбирать за обоих.

Почему настройка одна на весь набор событий, а не на каждый чат в отдельности,
если чат ничего не менял: у уровня, который ничего не менял, записи нет вовсе, и
он показывает — прямо в интерфейсе, а не в документации — от кого унаследовал.

Настройки уведомлений живут ТОЛЬКО здесь. Второй вход в ту же настройку (команда
в боте, экран в мини-аппе) означал бы три реализации одной цепочки наследования,
которые разойдутся, — по той же причине, по которой в мини-апп не переехал
`/bind`.
"""
from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from b24bot.api import ui_kit as ui
from b24bot.core.text import esc_attr, esc_html
from b24bot.db.pool import pool
from b24bot.domain import audit, notifications

log = logging.getLogger(__name__)
router = APIRouter(prefix="/b24/app", tags=["bitrix24-app"])

INHERIT = "inherit"
_SCOPE_TITLE = {"tenant": "теннанта", "project": "проекта", "binding": "чата"}


# --------------------------------------------------------------------- экран
async def render_block(tenant_id: int, can_manage: bool, session: str,
                       active: str = "notify") -> str:
    async with pool().acquire() as conn:
        projects = await conn.fetch(
            """
            SELECT p.id, p.name, c.name AS client
              FROM projects p JOIN clients c ON c.id = p.client_id
             WHERE p.tenant_id = $1 AND p.status = 'active'
             ORDER BY c.name, p.name
            """, tenant_id)
        bindings = await conn.fetch(
            """
            SELECT b.id, b.project_id, ch.title, ch.chat_id, t.thread_id
              FROM chat_bindings b
              JOIN tg_chats ch ON ch.id = b.chat_ref
              LEFT JOIN tg_topics t ON t.id = b.topic_ref
             WHERE b.tenant_id = $1 AND b.status = 'active'
             ORDER BY ch.first_seen_at
            """, tenant_id)

    ev = {kind: await notifications.all_scope_events(tenant_id, kind)
          for kind in notifications.SCOPES}
    mins = {kind: await notifications.all_scope_minutes(tenant_id, kind)
            for kind in notifications.SCOPES}

    tenant_rules = _effective(ev["tenant"].get(0), None, None,
                              mins["tenant"].get(0), None, None)
    tail = ("" if can_manage else
            ui.hint("Настраивать уведомления может администратор теннанта."))
    head = ui.panel(
        "Уведомления всего теннанта",
        _scope_form(session, active, "tenant", 0, ev["tenant"].get(0),
                    mins["tenant"].get(0), tenant_rules, can_manage),
        icon_name="alert", footer_html=tail)

    if not projects:
        return head + ui.panel(
            "Проекты и чаты",
            ui.empty("Проектов пока нет",
                     "Отдельные настройки появятся, когда хотя бы один проект "
                     "будет привязан к чату во вкладке «Чаты».",
                     icon_name="folder"),
            icon_name="folder")

    by_project: dict[int, list[asyncpg.Record]] = {}
    for b in bindings:
        by_project.setdefault(int(b["project_id"]), []).append(b)

    blocks = "".join(
        _project_block(session, active, p, by_project.get(int(p["id"]), []),
                       ev, mins, tenant_rules, can_manage)
        for p in projects)
    note = (f'<span class="panel-note tnum">проектов: {len(projects)} · '
            f"чатов с привязкой: {len(bindings)}</span>")
    return head + ui.panel("Проекты и чаты", blocks, icon_name="folder", flush=True,
                           actions_html=note)


def _effective(tenant_ev: dict[str, bool] | None, project_ev: dict[str, bool] | None,
               binding_ev: dict[str, bool] | None, tenant_min: int | None,
               project_min: int | None, binding_min: int | None
               ) -> notifications.Resolved:
    """Что применится на этом уровне — тем же кодом, что и при отправке.

    Показывать здесь свой пересчёт цепочки значило бы завести второе место, где
    она может разойтись с настоящей. Экран, уверенно показывающий не то, что
    происходит в чате, хуже отсутствия экрана.
    """
    ev_rows = [(kind, code, value)
               for kind, table in (("tenant", tenant_ev), ("project", project_ev),
                                   ("binding", binding_ev)) if table
               for code, value in table.items()]
    dg_rows = [(kind, value)
               for kind, value in (("tenant", tenant_min), ("project", project_min),
                                   ("binding", binding_min)) if value is not None]
    return notifications.resolve_rows(ev_rows, dg_rows)


def _summary(rules: notifications.Resolved) -> str:
    """Строка «что сейчас происходит в этом чате» — числами, а не словами."""
    on = sum(1 for e in notifications.EVENTS
             if e.emitted and rules.enabled.get(e.code, False))
    total = len(notifications.EMITTED)
    return (f"событий: {on} из {total} · "
            f"{notifications.interval_label(rules.minutes)}")


def _origin(kind: str) -> str:
    if kind == "default":
        return "системные значения"
    return f"настройка {_SCOPE_TITLE[kind]}"


def _project_block(session: str, active: str, project: asyncpg.Record,
                   bindings: list[asyncpg.Record],
                   ev: dict[str, dict[int, dict[str, bool]]],
                   mins: dict[str, dict[int, int]],
                   tenant_rules: notifications.Resolved, can_manage: bool) -> str:
    pid = int(project["id"])
    p_ev, p_min = ev["project"].get(pid), mins["project"].get(pid)
    rules = _effective(ev["tenant"].get(0), p_ev, None, mins["tenant"].get(0), p_min,
                       None)

    own = p_ev is not None or p_min is not None
    badge = (ui.badge("своя настройка", "info") if own
             else ui.badge("как у теннанта", "neutral"))
    head = (f'<div class="chat-h"><div class="chat-meta">'
            f'<span class="chat-name">{esc_html(project["name"])}</span>'
            f'<span class="chat-id">клиент {esc_html(project["client"])} · '
            f'{esc_html(_summary(rules))}</span></div>{badge}</div>')

    if not can_manage:
        return f'<div class="chat-block">{head}</div>'

    body = _details(f"Настроить проект «{project['name']}»",
                    _scope_form(session, active, "project", pid, p_ev, p_min, rules,
                                can_manage, inherited=tenant_rules))

    for b in bindings:
        bid = int(b["id"])
        b_ev, b_min = ev["binding"].get(bid), mins["binding"].get(bid)
        chat_rules = _effective(ev["tenant"].get(0), p_ev, b_ev, mins["tenant"].get(0),
                                p_min, b_min)
        chat_name = b["title"] or f"чат {b['chat_id']}"
        topic = " · тема форума" if b["thread_id"] else ""
        state = (ui.badge("своя настройка", "info")
                 if b_ev is not None or b_min is not None
                 else ui.badge("как у проекта", "neutral"))
        body += (
            f'<div class="proj"><div class="proj-m">'
            f'<span class="proj-ico">{ui.icon("chat", 15)}</span>'
            f'<div><div class="proj-t">{esc_html(chat_name)}{esc_html(topic)}</div>'
            f'<div class="proj-s">{esc_html(_summary(chat_rules))}</div></div>'
            f'</div><div class="item-a">{state}</div></div>'
            + _details(f"Настроить чат «{chat_name}»",
                       _scope_form(session, active, "binding", bid, b_ev, b_min,
                                   chat_rules, can_manage, inherited=rules)))
    if not bindings:
        body += ('<div class="proj-none">Чатов у проекта нет — настраивать пока '
                 "нечего</div>")
    return f'<div class="chat-block">{head}<div class="chat-body">{body}</div></div>'


def _details(summary: str, body: str) -> str:
    """Раскрывашка. Закрыта по умолчанию: настройка — редкое действие, а список
    проектов и чатов нужен целиком и сразу."""
    return (f'<details class="bind"><summary>{ui.icon("settings", 15)}'
            f"{esc_html(summary)}</summary>{body}</details>")


def _scope_form(session: str, active: str, scope_kind: str, scope_id: int,
                explicit: dict[str, bool] | None, minutes: int | None,
                rules: notifications.Resolved, can_manage: bool,
                inherited: notifications.Resolved | None = None) -> str:
    """Форма одного уровня: набор событий и интервал группировки.

    Галочки всегда показывают то, что применяется СЕЙЧАС, — унаследованное в том
    числе. Поэтому «настроить отдельно» без единого клика по галочкам сохраняет
    ровно то, что человек и видел: настройка перестаёт наследоваться, но ничего
    не меняет. Пустая форма на этом месте выглядела бы как «всё выключено».
    """
    if not can_manage:
        return ui.hint("Настраивать уведомления может администратор теннанта.")

    ident = f"{scope_kind}-{scope_id}"
    checks = "".join(
        ui.checkbox("ev", e.code, e.label, hint=e.hint,
                    checked=rules.enabled.get(e.code, e.default))
        for e in notifications.EVENTS if e.emitted)

    if scope_kind == "tenant":
        mode_options = [("custom", "свой набор — отмеченный ниже"),
                        (INHERIT, "системные значения по умолчанию")]
        digest_first = []
    else:
        upper = _SCOPE_TITLE["tenant" if scope_kind == "project" else "project"]
        mode_options = [(INHERIT, f"как у {upper} — наследовать"),
                        ("custom", "свой набор — отмеченный ниже")]
        digest_first = [(INHERIT, f"как у {upper} — наследовать")]

    mode = "custom" if explicit is not None else INHERIT
    mode_html = "".join(
        f'<option value="{value}"{" selected" if value == mode else ""}>'
        f"{esc_html(label)}</option>" for value, label in mode_options)

    current = INHERIT if minutes is None else str(minutes)
    digest_html = "".join(
        f'<option value="{esc_attr(value)}"'
        f'{" selected" if str(value) == current else ""}>{esc_html(label)}</option>'
        for value, label in [*digest_first,
                             *((str(m), label) for m, label in notifications.INTERVALS)])

    inherit_note = ""
    if inherited is not None:
        inherit_note = ui.hint(
            f"Если наследовать: {_summary(inherited)}.")
    origin = ui.hint(
        f"Сейчас применяется: {_summary(rules)}. Набор событий — "
        f"{_origin(rules.events_from)}, интервал — {_origin(rules.minutes_from)}.")

    return (
        f'<form method="post" action="/b24/app/notify">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        f'<input type="hidden" name="tab" value="{esc_attr(active)}">'
        f'<input type="hidden" name="scope_kind" value="{esc_attr(scope_kind)}">'
        f'<input type="hidden" name="scope_id" value="{scope_id}">'
        f"{origin}{inherit_note}"
        f'<div class="grid2" style="margin-top:12px">'
        f'<div class="f-group">'
        f'<label class="f-l" for="nm-{esc_attr(ident)}">Набор событий</label>'
        f'<select class="input" id="nm-{esc_attr(ident)}" name="mode">{mode_html}'
        f"</select></div>"
        f'<div class="f-group">'
        f'<label class="f-l" for="nd-{esc_attr(ident)}">Группировка</label>'
        f'<select class="input" id="nd-{esc_attr(ident)}" name="digest">{digest_html}'
        f"</select></div></div>"
        f'<fieldset style="border:0;padding:0;margin:0 0 14px">'
        f'<legend class="f-l" style="padding:0">О чём сообщать</legend>'
        f'<div class="checks">{checks}</div></fieldset>'
        f'<div class="btn-row">'
        f'<button class="btn sec" type="submit">{ui.icon("check", 15)}'
        f"Сохранить</button></div>"
        f'<p class="hint">Группировка копит уведомления и присылает их одной '
        f"сводкой. Окно начинается с первой новости: включённые «раз в 15 минут» "
        f"не задерживают сообщение на 15 минут, если за это время больше ничего "
        f"не произошло — сводка из одной новости уходит обычным уведомлением, "
        f"с кнопками.</p>"
        f"</form>")


# ------------------------------------------------------------------ сохранение
@router.post("/notify")
async def save_notify(request: Request, session: str = Form(...),
                      scope_kind: str = Form(...), scope_id: int = Form(0),
                      mode: str = Form(INHERIT), digest: str = Form(INHERIT),
                      tab: str = Form("notify")) -> HTMLResponse:
    from b24bot.api import app_ui

    # Отмеченные галочки приезжают повторяющимся полем `ev`, поэтому читаются из
    # формы целиком: объявить их отдельным аргументом-списком нельзя без
    # изменяемого значения по умолчанию.
    codes = [str(value) for value in (await request.form()).getlist("ev")]

    sess = await app_ui.load_session(session)
    if sess is None:
        return app_ui.expired_page()

    tab = app_ui.safe_tab(tab)
    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants "
            "WHERE id = $1", sess["tenant_id"])
    tenant_id, actor = int(tenant["id"]), int(sess["b24_user_id"])
    is_portal_admin = bool(sess["is_portal_admin"])

    if not await app_ui.can_manage_admins(tenant_id, actor, is_portal_admin):
        log.warning("попытка настроить уведомления без прав: теннант %s, "
                    "пользователь %s", tenant_id, actor)
        message, kind = ("Настраивать уведомления может администратор теннанта.",
                         "err")
    else:
        message, kind = await _apply(tenant_id, actor, scope_kind, scope_id, mode,
                                     digest, codes)

    async with pool().acquire() as conn:
        fresh = await app_ui.issue_session(conn, tenant_id, actor, is_portal_admin)
    body = await app_ui.render_home(tenant, actor, is_portal_admin, fresh,
                                    message=message, message_kind=kind, active_tab=tab)
    return app_ui.page(body, tenant["b24_domain"])


async def _apply(tenant_id: int, actor: int, scope_kind: str, scope_id: int,
                 mode: str, digest: str, codes: list[str]) -> tuple[str, str]:
    """Проверить и записать. Всё, что приехало формой, проверяется заново.

    Уровень настройки приезжает из браузера пользователя портала, а значит может
    приехать любым: чужой проект, чужая привязка, несуществующий вид. Поэтому
    принадлежность теннанту сверяется по базе, а не по тому, что мы сами
    нарисовали на странице пять минут назад (И-2).
    """
    if scope_kind not in notifications.SCOPES:
        return "Неизвестный уровень настройки.", "err"

    name = await _scope_name(tenant_id, scope_kind, scope_id)
    if name is None:
        return ("Проект или чат не найден — возможно, привязку изменили. "
                "Обновите страницу."), "err"

    minutes: int | None
    if digest == INHERIT:
        # На уровне теннанта наследовать не у кого: там «наследовать» означало бы
        # системное значение, а оно и есть «сразу», — молча писать 0 честнее,
        # чем оставлять уровень без ответа.
        minutes = None if scope_kind != "tenant" else notifications.DIGEST_DEFAULT
    else:
        try:
            minutes = int(digest)
        except ValueError:
            return "Интервал группировки не распознан.", "err"
        if minutes not in notifications.INTERVAL_MINUTES:
            return "Такого интервала группировки нет в списке.", "err"

    enabled: dict[str, bool] | None
    if mode == INHERIT:
        enabled = None
    else:
        picked = {c for c in codes if c in notifications.EMITTED}
        enabled = {code: code in picked for code in notifications.EMITTED}

    await notifications.save_events(tenant_id, scope_kind, scope_id, enabled)
    await notifications.save_minutes(tenant_id, scope_kind, scope_id, minutes)

    await audit.record(
        tenant_id, "notify.configure", actor_id=actor,
        project_id=scope_id if scope_kind == "project" else None,
        target=f"{scope_kind}:{scope_id}",
        detail={"уровень": scope_kind, "объект": name,
                "события": "наследовать" if enabled is None
                           else sorted(c for c, on in enabled.items() if on),
                "группировка": "наследовать" if minutes is None
                               else notifications.interval_label(minutes)})

    where = "для всего теннанта" if scope_kind == "tenant" else f"для «{name}»"
    return f"Настройки уведомлений {esc_html(where)} сохранены.", "ok"


async def _scope_name(tenant_id: int, scope_kind: str, scope_id: int) -> str | None:
    """Человеческое имя уровня — и одновременно проверка, что он наш."""
    if scope_kind == "tenant":
        return "теннант"
    async with pool().acquire() as conn:
        if scope_kind == "project":
            value = await conn.fetchval(
                "SELECT name FROM projects WHERE id = $1 AND tenant_id = $2",
                scope_id, tenant_id)
            return str(value) if value is not None else None
        row = await conn.fetchrow(
            "SELECT p.name AS project, ch.title, ch.chat_id "
            "  FROM chat_bindings b "
            "  JOIN projects p ON p.id = b.project_id "
            "  JOIN tg_chats ch ON ch.id = b.chat_ref "
            " WHERE b.id = $1 AND b.tenant_id = $2", scope_id, tenant_id)
    if row is None:
        return None
    chat = row["title"] or f"чат {row['chat_id']}"
    return f"{chat} — {row['project']}"
