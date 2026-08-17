"""Настройка подтверждения задач ответственным — вкладка приложения Б24.

Список проектов и форма настройки живут прямо во вкладке (как «Чаты»), а не на
отдельной странице (как конструктор опросника): здесь один уровень вложенности —
проект и его четыре поля, а не многошаговый редактор вопросов.

Стадии канбана — ЖИВОЙ список с портала (invariant «список для выбора берётся
с портала»), запрашиваются одним batch-вызовом на все проекты разом, а не по
одному: на теннанте с десятком проектов последовательные вызовы упёрлись бы
в частотный лимит заметнее, чем на любом другом экране приложения.
"""
from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

from b24bot.api import ui_kit as ui
from b24bot.core.text import esc_attr, esc_html
from b24bot.db.pool import pool
from b24bot.domain import access, approvals, audit, sync

log = logging.getLogger(__name__)
router = APIRouter(prefix="/b24/app", tags=["bitrix24-app"])


async def render_block(tenant_id: int, b24_user_id: int, can_manage: bool,
                       session: str, active: str = "approval") -> str:
    async with pool().acquire() as conn:
        projects = await conn.fetch(
            """
            SELECT p.id, p.b24_group_id, p.name, c.name AS client
              FROM projects p JOIN clients c ON c.id = p.client_id
             WHERE p.tenant_id = $1 AND p.status = 'active'
             ORDER BY c.name, p.name
            """, tenant_id)
        members = await conn.fetch(
            """
            SELECT m.user_id, u.display_name, u.tg_username
              FROM tenant_members m JOIN users u ON u.id = m.user_id
             WHERE m.tenant_id = $1 AND m.link_status = 'authorized'
             ORDER BY u.display_name
            """, tenant_id)

    if not projects:
        return ui.panel(
            "Подтверждение задач",
            ui.empty("Проектов пока нет",
                     "Настройка появится, когда хотя бы один проект будет привязан "
                     "к чату во вкладке «Чаты» — стадии канбана берутся у него.",
                     icon_name="check-circle"),
            icon_name="check-circle")

    settings = await approvals.settings_for_projects(
        tenant_id, [int(p["id"]) for p in projects])

    stages_by_project: dict[int, list[sync.Stage]] = {}
    stages_error = ""
    try:
        client = await access.client_for_user(tenant_id, b24_user_id)
        async with client:
            raw = await client.call_many(
                [(str(p["id"]), "task.stages.get", {"entityId": int(p["b24_group_id"])})
                 for p in projects])
        for p in projects:
            pid = int(p["id"])
            stages_by_project[pid] = sync.parse_stages(raw.get(str(pid)),
                                                       int(p["b24_group_id"]))
    except Exception as exc:
        log.warning("не удалось получить стадии канбана для настройки "
                   "подтверждения: %s", str(exc)[:150])
        stages_error = ("Битрикс24 не ответил на запрос стадий канбана — выбор "
                        "стадии сейчас недоступен, попробуйте обновить страницу.")

    notes = ui.banner(esc_html(stages_error), "warn") if stages_error else ""
    if not members:
        notes += ui.banner(
            "Пока никто не привязал Telegram — назначить ответственного не из "
            "кого. Список пополнится сам, как только кто-то привяжет аккаунт "
            "во вкладке «Команда».", "info")

    body = "".join(
        _project_row(session, active, int(p["id"]), p["name"], p["client"],
                    settings.get(int(p["id"])), members,
                    stages_by_project.get(int(p["id"]), []), can_manage)
        for p in projects)

    tail = ("" if can_manage else
            ui.hint("Настраивать подтверждение задач может администратор теннанта."))
    return (notes
            + ui.panel("Подтверждение задач", body, icon_name="check-circle",
                      flush=True, footer_html=tail))


def _project_row(session: str, active: str, project_id: int, name: str, client: str,
                 s: approvals.Settings | None, members: list[asyncpg.Record],
                 stages: list[sync.Stage], can_manage: bool) -> str:
    enabled = bool(s and s.enabled)
    status = ui.badge("включено", "ok") if enabled else ui.badge("выключено", "neutral")
    head = (f'<div class="chat-h"><div class="chat-meta">'
           f'<span class="chat-name">{esc_html(name)}</span>'
           f'<span class="chat-id">клиент {esc_html(client)}</span></div>{status}</div>')

    if not can_manage:
        return f'<div class="chat-block">{head}</div>'

    if not stages:
        return (f'<div class="chat-block">{head}<div class="chat-body">'
               + ui.hint("Стадии канбана этого проекта сейчас недоступны.")
               + "</div></div>")

    resp_id = s.responsible_user_id if s else None
    member_opts = ['<option value="0">— выбрать человека —</option>']
    for m in members:
        sel = " selected" if resp_id == m["user_id"] else ""
        label = (m["display_name"] or
                 (f"@{m['tg_username']}" if m["tg_username"]
                  else f"пользователь {m['user_id']}"))
        member_opts.append(f'<option value="{m["user_id"]}"{sel}>'
                          f"{esc_html(label)}</option>")

    def stage_options(current: int | None) -> str:
        opts = ['<option value="0">— выбрать стадию —</option>']
        for st in stages:
            sel = " selected" if current == st.b24_stage_id else ""
            opts.append(f'<option value="{st.b24_stage_id}"{sel}>'
                       f"{esc_html(st.title)}</option>")
        return "".join(opts)

    pid = project_id
    body = (
        f'<form method="post" action="/b24/app/approval">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        f'<input type="hidden" name="tab" value="{esc_attr(active)}">'
        f'<input type="hidden" name="project_id" value="{pid}">'
        f'<div class="grid2">'
        f'<div class="f-group">'
        f'<label class="f-l" for="ap-en-{pid}">Требовать подтверждение</label>'
        f'<select class="input" id="ap-en-{pid}" name="enabled">'
        f'<option value="0"{"" if enabled else " selected"}>нет</option>'
        f'<option value="1"{" selected" if enabled else ""}>да</option>'
        f"</select></div>"
        f'<div class="f-group">'
        f'<label class="f-l" for="ap-resp-{pid}">Ответственный</label>'
        f'<select class="input" id="ap-resp-{pid}" name="responsible_user_id">'
        f'{"".join(member_opts)}</select></div>'
        f"</div>"
        f'<div class="grid2">'
        f'<div class="f-group">'
        f'<label class="f-l" for="ap-ok-{pid}">Стадия «подтверждена»</label>'
        f'<select class="input" id="ap-ok-{pid}" name="confirm_stage_id">'
        f'{stage_options(s.confirm_stage_id if s else None)}</select></div>'
        f'<div class="f-group">'
        f'<label class="f-l" for="ap-no-{pid}">Стадия «отклонена»</label>'
        f'<select class="input" id="ap-no-{pid}" name="reject_stage_id">'
        f'{stage_options(s.reject_stage_id if s else None)}</select></div>'
        f"</div>"
        f'<div class="btn-row" style="margin-top:4px">'
        f'<button class="btn sec" type="submit">{ui.icon("check", 15)}'
        f"Сохранить</button></div>"
        f'<p class="hint">При создании новой задачи в этом проекте ответственный '
        f"получит в личке с ботом кнопки «Подтвердить»/«Отклонить». Решение "
        f"передвинет задачу на выбранную стадию.</p>"
        f"</form>")
    return f'<div class="chat-block">{head}<div class="chat-body">{body}</div></div>'


# ------------------------------------------------------------------ сохранение
@router.post("/approval")
async def save_approval(session: str = Form(...), project_id: int = Form(...),
                        enabled: str = Form("0"), responsible_user_id: int = Form(0),
                        confirm_stage_id: int = Form(0), reject_stage_id: int = Form(0),
                        tab: str = Form("approval")) -> HTMLResponse:
    from b24bot.api import app_ui

    sess = await app_ui.load_session(session)
    if sess is None:
        return app_ui.expired_page()

    tab = app_ui.safe_tab(tab)
    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants WHERE id = $1",
            sess["tenant_id"])
    tenant_id, actor = int(tenant["id"]), int(sess["b24_user_id"])
    is_portal_admin = bool(sess["is_portal_admin"])

    if not await app_ui.can_manage_admins(tenant_id, actor, is_portal_admin):
        log.warning("попытка настроить подтверждение задач без прав: теннант %s, "
                   "пользователь %s", tenant_id, actor)
        message, kind = ("Настраивать подтверждение задач может администратор "
                         "теннанта.", "err")
    else:
        message, kind = await _apply(
            tenant_id, actor, project_id, enabled == "1", responsible_user_id or None,
            confirm_stage_id or None, reject_stage_id or None)

    async with pool().acquire() as conn:
        fresh = await app_ui.issue_session(conn, tenant_id, actor, is_portal_admin)
    body = await app_ui.render_home(tenant, actor, is_portal_admin, fresh,
                                    message=message, message_kind=kind, active_tab=tab)
    return app_ui.page(body, tenant["b24_domain"])


async def _apply(tenant_id: int, actor: int, project_id: int, enabled: bool,
                 responsible_user_id: int | None, confirm_stage_id: int | None,
                 reject_stage_id: int | None) -> tuple[str, str]:
    async with pool().acquire() as conn:
        project = await conn.fetchrow(
            "SELECT b24_group_id, name FROM projects WHERE id = $1 AND tenant_id = $2",
            project_id, tenant_id)
    if project is None:
        return "Проект не найден.", "err"

    if enabled and not (responsible_user_id and confirm_stage_id and reject_stage_id):
        return ("Чтобы включить подтверждение, выберите ответственного и обе "
                "стадии.", "err")
    if confirm_stage_id and confirm_stage_id == reject_stage_id:
        return "Стадии для подтверждения и отклонения должны различаться.", "err"

    titles: dict[int, str] = {}
    if confirm_stage_id or reject_stage_id:
        try:
            client = await access.client_for_user(tenant_id, actor)
            async with client:
                raw = await client.call("task.stages.get",
                                        {"entityId": int(project["b24_group_id"])})
        except Exception as exc:
            log.warning("не удалось перечитать стадии канбана проекта %s: %s",
                       project_id, str(exc)[:150])
            return "Битрикс24 не ответил — попробуйте сохранить ещё раз.", "err"
        stages = sync.parse_stages(raw, int(project["b24_group_id"]))
        titles = {st.b24_stage_id: st.title for st in stages}
        # Сверка с ЖИВЫМ списком, а не со значением из формы: стадию могли
        # удалить на портале между открытием страницы и отправкой формы.
        if confirm_stage_id and confirm_stage_id not in titles:
            return ("Стадия «подтверждена» не найдена среди стадий канбана — "
                    "обновите страницу.", "err")
        if reject_stage_id and reject_stage_id not in titles:
            return ("Стадия «отклонена» не найдена среди стадий канбана — "
                    "обновите страницу.", "err")

    if responsible_user_id:
        async with pool().acquire() as conn:
            linked = await conn.fetchval(
                "SELECT 1 FROM tenant_members WHERE tenant_id = $1 AND user_id = $2 "
                "AND link_status = 'authorized'", tenant_id, responsible_user_id)
        if linked is None:
            return "Выбранный человек не привязал Telegram.", "err"

    await approvals.upsert_settings(
        tenant_id, project_id, enabled=enabled, responsible_user_id=responsible_user_id,
        confirm_stage_id=confirm_stage_id,
        confirm_stage_title=titles.get(confirm_stage_id or 0, ""),
        reject_stage_id=reject_stage_id,
        reject_stage_title=titles.get(reject_stage_id or 0, ""))
    await audit.record(tenant_id, "task.approval.configure", actor_id=actor,
                       project_id=project_id, target=f"project:{project_id}",
                       detail={"проект": project["name"], "enabled": enabled})
    return (f"Настройки подтверждения для «{esc_html(project['name'])}» сохранены.", "ok")
