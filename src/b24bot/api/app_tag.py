"""Тег поддержки — блок на вкладке «Бот» приложения Б24.

Одна настройка на теннанта: тег, который дописывается каждой задаче, созданной
через бота или мини-апп. По нему потом отбираются задачи поддержки и режется
отчёт о трудозатратах.

Настройка живёт здесь и только здесь. Второй вход (команда в боте, поле в
мини-аппе) означал бы два места, где её меняют, и ни одного, где видно текущее
значение вместе с последствиями — а последствия тут ровно те, из-за которых
настройку и заводили: изменил тег — прежние задачи перестали попадать в отчёт.

**Разовый проход** дописывает тег задачам, созданным до появления настройки. Мы
точно знаем, какие задачи создали: они все записаны в `entity_external_refs` при
создании (И-10). Проход идёт read-modify-write по каждой задаче, потому что
`tasks.task.update` с `TAGS` задаёт набор ЦЕЛИКОМ, и отправка одного нового тега
стёрла бы ключ идемпотентности (docs/00-portal-facts.md §5.4).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

from b24bot.api import ui_kit as ui
from b24bot.b24 import errors
from b24bot.b24.client import B24Client
from b24bot.b24.limiter import Lane
from b24bot.b24.tokens import NeedsReauth
from b24bot.core.text import esc_attr, esc_html
from b24bot.db.pool import pool
from b24bot.domain import access, audit, support_tag

log = logging.getLogger(__name__)
router = APIRouter(prefix="/b24/app", tags=["bitrix24-app"])

BACKFILL_LIMIT = 200
"""Потолок одного прохода. Каждая задача — это чтение и запись, то есть два
вызова портала против `operating`-лимита (420 с в окне 600 с). Больше двухсот
за раз означает не «медленно», а «упёрлись в лимит и половина не доехала»."""


# --------------------------------------------------------------------- экран
async def render_block(tenant_id: int, can_manage: bool, session: str,
                       active: str = "bot") -> str:
    """Выборка. Разметку собирает `panel()` — её меряет стенд вёрстки."""
    tag = await support_tag.get(tenant_id)
    synced = await support_tag.synced_at(tenant_id)

    async with pool().acquire() as conn:
        total = await conn.fetchval(
            "SELECT count(*) FROM entity_external_refs "
            "WHERE tenant_id = $1 AND target_kind = 'b24_task' AND state = 'committed'",
            tenant_id)

    return panel(tag, bool(synced), int(total or 0), can_manage, session, active)


def panel(tag: str, synced: bool, created: int, can_manage: bool,
          session: str, active: str = "bot") -> str:
    """Разметка блока — чистая функция.

    Отдельно от выборки затем, что вёрстка меряется, а не осматривается:
    `scripts/audit_b24app.mjs` собирает превью из БОЕВЫХ компонентов и проверяет
    контраст, цели касания и переполнение. Данные из базы в стенде взять неоткуда,
    и панель, которую нельзя построить без неё, осталась бы неизмеренной.
    """
    rows = ui.field(
        "Текущий тег",
        f"<code>{esc_html(tag)}</code>" if tag
        else ui.badge("пометка выключена", "warn"))
    rows += ui.field("Задач заведено через бота", f"{created}")
    rows += ui.field(
        "Разовый проход по старым задачам",
        ui.badge("выполнен", "ok") if synced else ui.badge("не выполнялся", "warn"))

    if not can_manage:
        return ui.panel("Тег поддержки", rows, icon_name="inbox")

    form = (
        f'<form method="post" action="/b24/app/support-tag">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        f'<input type="hidden" name="tab" value="{esc_attr(active)}">'
        f'<div class="f-group">'
        f'<label class="f-l" for="support-tag">Тег на задачах из Telegram</label>'
        f'<input class="input mono" type="text" id="support-tag" name="tag" '
        f'value="{esc_attr(tag)}" placeholder="tg-support" maxlength="'
        f'{support_tag.TAG_MAX}" autocomplete="off" spellcheck="false" '
        f'aria-describedby="support-tag-h">'
        f'<p class="hint" id="support-tag-h">Ставится каждой новой задаче рядом '
        f"с остальными её тегами. По нему в отчёте «Трудозатраты» считается "
        f"строка «Поддержка». Пустое поле — не помечать задачи и не делить "
        f"отчёт. Задачи, помеченные прежним тегом, после смены в эту строку "
        f"попадать перестанут.</p></div>"
        f'<div class="btn-row"><button class="btn sec" type="submit">'
        f'{ui.icon("check", 15)}Сохранить тег</button></div></form>')

    backfill = ui.action_form(
        "/b24/app/support-tag/backfill",
        {"session": session, "tab": active},
        "Проставить тег ранее созданным задачам",
        icon_name="renew",
        disabled=not tag,
        title="" if tag else "Сначала задайте тег",
        confirm=(f"Тег «{tag}» будет дописан задачам, заведённым через бота "
                 f"(до {BACKFILL_LIMIT} за раз). Существующие теги задач "
                 f"сохранятся. Продолжить?") if tag else "",
        inline=False)

    note = ui.note(
        "Без этого прохода трудозатраты по задачам, заведённым до появления "
        "настройки, в строку «Поддержка» не попадут: тега на них нет.",
        "warn" if not synced else "neutral")

    return ui.panel("Тег поддержки",
                    rows + '<div class="divider"></div>' + form
                    + '<div class="divider"></div>' + note + backfill,
                    icon_name="inbox")


# -------------------------------------------------------------------- запись
@router.post("/support-tag")
async def save_tag(session: str = Form(...), tag: str = Form(""),
                   tab: str = Form("bot")) -> HTMLResponse:
    from b24bot.api import app_ui

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
        message, kind = "Менять тег может администратор теннанта.", "err"
    else:
        try:
            saved = await support_tag.set_tag(tenant_id, tag)
        except support_tag.InvalidTag as exc:
            message, kind = f"Тег не сохранён: {exc}.", "err"
        else:
            await audit.record(tenant_id, "tenant.support_tag.set", actor_id=actor,
                               target=f"tenant:{tenant_id}", detail={"tag": saved})
            message = (f"Тег сохранён: {saved}." if saved
                       else "Пометка задач выключена.")
            kind = "ok"

    async with pool().acquire() as conn:
        fresh = await app_ui.issue_session(conn, tenant_id, actor, is_portal_admin)
    body = await app_ui.render_home(tenant, actor, is_portal_admin, fresh,
                                    message=message, message_kind=kind, active_tab=tab)
    return app_ui.page(body, tenant["b24_domain"])


@router.post("/support-tag/backfill")
async def run_backfill(session: str = Form(...),
                       tab: str = Form("bot")) -> HTMLResponse:
    from b24bot.api import app_ui

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
        message, kind = "Запускать проход может администратор теннанта.", "err"
    else:
        message, kind = await _backfill(tenant_id, actor)

    async with pool().acquire() as conn:
        fresh = await app_ui.issue_session(conn, tenant_id, actor, is_portal_admin)
    body = await app_ui.render_home(tenant, actor, is_portal_admin, fresh,
                                    message=message, message_kind=kind, active_tab=tab)
    return app_ui.page(body, tenant["b24_domain"])


async def _backfill(tenant_id: int, actor: int) -> tuple[str, str]:
    """Дописать тег задачам, которые создал бот.

    Токен — личный, того, кто нажал: править чужие задачи сервисным аккаунтом
    значило бы обходить права портала, а не пользоваться ими.
    """
    tag = await support_tag.get(tenant_id)
    if not tag:
        return "Сначала задайте тег.", "err"

    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT target_id FROM entity_external_refs "
            "WHERE tenant_id = $1 AND target_kind = 'b24_task' "
            "AND state = 'committed' AND target_id IS NOT NULL "
            "ORDER BY target_id DESC LIMIT $2", tenant_id, BACKFILL_LIMIT + 1)
    task_ids = [int(r["target_id"]) for r in rows]
    more = len(task_ids) > BACKFILL_LIMIT
    task_ids = task_ids[:BACKFILL_LIMIT]
    if not task_ids:
        return "Задач, заведённых через бота, пока нет.", "warn"

    try:
        client = await access.client_for_user(tenant_id, actor)
    except NeedsReauth:
        return ("Истёк доступ к Битрикс24. Откройте приложение заново.", "err")

    tagged = skipped = failed = 0
    async with client:
        for task_id in task_ids:
            try:
                outcome = await _tag_one(client, task_id, tag)
            except errors.B24Error as exc:
                # Одна недоступная задача не должна останавливать проход: её могли
                # удалить или закрыть от этого человека. Число отказов — в ответе.
                log.warning("тег не проставлен задаче %s: %s", task_id, exc)
                failed += 1
                continue
            if outcome:
                tagged += 1
            else:
                skipped += 1

    await audit.record(tenant_id, "tenant.support_tag.backfill", actor_id=actor,
                       target=f"tenant:{tenant_id}",
                       detail={"tag": tag, "tagged": tagged, "skipped": skipped,
                               "failed": failed, "more": more})
    if tagged or skipped:
        await support_tag.mark_synced(tenant_id)

    parts = [f"Проставлен тег: {tagged}", f"уже был: {skipped}"]
    if failed:
        parts.append(f"не удалось: {failed}")
    if more:
        # Молчаливое усечение читается как «сделано всё». Говорим числом и
        # называем, что делать дальше.
        parts.append(f"обработано не больше {BACKFILL_LIMIT} за раз — "
                     f"нажмите ещё раз для остальных")
    return ". ".join(parts) + ".", "err" if failed and not tagged else "ok"


async def _tag_one(client: B24Client, task_id: int, tag: str) -> bool:
    """Дописать тег одной задаче. True — записали, False — тег уже был."""
    res = await client.call("tasks.task.get",
                            {"taskId": task_id, "select": ["ID", "TAGS"]},
                            lane=Lane.BACKGROUND)
    task = res.get("task", res) if isinstance(res, dict) else {}
    merged = support_tag.merged(task.get("tags"), tag)
    if merged is None:
        return False
    await client.call("tasks.task.update",
                      {"taskId": task_id, "fields": {"TAGS": merged}},
                      lane=Lane.BACKGROUND)
    return True
