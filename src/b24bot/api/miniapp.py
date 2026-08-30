"""HTTP-контракт мини-аппа.

Набор операций тот же, что у бота (docs/50-web-and-b24-app.md): расходятся такие
интерфейсы ровно там, где их пишут по отдельности. Поэтому здесь нет ни одной
операции с задачей, которая обходила бы `authorize_task_for_chat` (И-3), и ни
одного обращения к порталу не личным токеном человека.

Аутентификация — заголовок `Authorization: tma <initData>` на каждом запросе.
Своей сессии нет намеренно: строка Telegram и так подписана и протухает, а лишняя
таблица сессий — это ещё одно место, где можно забыть `tenant_id` (И-2).
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from b24bot.b24 import disk, errors, mapping
from b24bot.b24.tokens import NeedsReauth
from b24bot.bot import comments as comments_service
from b24bot.bot import survey, task_create, views
from b24bot.core.text import bbcode_to_text, safe_filename
from b24bot.db.pool import pool
from b24bot.domain import (
    access,
    approvals,
    audit,
    lifecycle,
    miniapp,
    sync,
    timelog,
    timesheet,
)
from b24bot.domain import tasks as task_service
from b24bot.domain.context import (
    TASK_NOT_FOUND,
    ProjectRef,
    authorize_task_for_chat,
    remember_task,
)
from b24bot.tg import files as tg_files

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/miniapp", tags=["miniapp"])

FORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
LIST_LIMIT = 200
COMMENTS_LIMIT = 50

ACTIONS = {"complete": "tasks.task.complete", "start": "tasks.task.start",
           "pause": "tasks.task.pause", "defer": "tasks.task.defer",
           "renew": "tasks.task.renew"}
# Ожидаемый статус после действия — чтобы своё же изменение не вернулось
# уведомлением в тот же чат.
ACTION_STATUS = {"complete": 5, "start": 3, "pause": 2, "defer": 6, "renew": 2}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 details: dict[str, Any] | None = None) -> None:
        self.status, self.code, self.message = status, code, message
        self.details = details or {}
        super().__init__(message)


def error_response(exc: ApiError) -> JSONResponse:
    body: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details:
        body["details"] = exc.details
    return JSONResponse({"error": body}, status_code=exc.status)


# --------------------------------------------------------------- зависимости
def _init_data_of(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    scheme, _, value = raw.partition(" ")
    if scheme.lower() != "tma" or not value.strip():
        raise ApiError(401, "unauthenticated", "Откройте мини-апп из Telegram.")
    return value.strip()


async def actor_dep(request: Request) -> miniapp.Actor:
    try:
        actor = await miniapp.authenticate(_init_data_of(request))
    except miniapp.NotLinked as exc:
        raise ApiError(403, "not_linked",
                       "Telegram не связан с пользователем Битрикс24.",
                       {"bot": exc.bot_username}) from exc
    except miniapp.Unauthenticated as exc:
        raise ApiError(401, "unauthenticated",
                       "Не удалось подтвердить, что запрос из Telegram. "
                       "Закройте и откройте приложение заново.") from exc
    # Единственная точка входа всех запросов мини-аппа — поэтому гейт подписки
    # Маркета стоит здесь, а не в каждом обработчике.
    if await lifecycle.blocked(actor.tenant_id):
        raise ApiError(402, "license_expired",
                       "Подписка Битрикс24.Маркет на этом портале истекла. "
                       "Продлить её может администратор портала — данные и "
                       "настройки на месте.")
    return actor


ActorDep = Annotated[miniapp.Actor, Depends(actor_dep)]


async def context_dep(actor: ActorDep,
                      chat_ref: Annotated[int | None, Query()] = None,
                      ctx_param: Annotated[str | None, Query(alias="ctx")] = None
                      ) -> tuple[miniapp.Actor, miniapp.Context]:
    try:
        ctx = await miniapp.resolve_context(actor, chat_ref=chat_ref,
                                            packed_ctx=ctx_param)
    except miniapp.Forbidden as exc:
        raise ApiError(403, "forbidden", str(exc)) from exc
    if ctx is None:
        raise ApiError(400, "validation", "Не выбран чат.")
    if not ctx.projects:
        raise ApiError(409, "no_project",
                       "К этому чату не привязан ни один проект Битрикс24.")
    return actor, ctx


CtxDep = Annotated[tuple[miniapp.Actor, miniapp.Context], Depends(context_dep)]
JsonBody = Annotated[dict[str, Any], Body(default_factory=dict)]


async def _client(actor: miniapp.Actor) -> Any:
    return await access.client_for_user(actor.tenant_id, actor.b24_user_id,
                                        actor_tg_user_id=actor.tg_user_id)


# ------------------------------------------------------------------- разбор
def _project_of(ctx: miniapp.Context, group_id: Any) -> ProjectRef | None:
    gid = mapping.as_int(group_id)
    return next((p for p in ctx.projects if p.b24_group_id == gid), None)


def _task_json(task: dict[str, Any], project: ProjectRef | None,
               depth: int = 0) -> dict[str, Any]:
    status = mapping.as_int(task.get("status"))
    return {
        "id": mapping.as_int(task.get("id")),
        "title": task.get("title") or "",
        "status": status,
        "status_title": mapping.STATUS_TITLES.get(status or 0, "—"),
        "status_emoji": mapping.STATUS_EMOJI.get(status or 0, "•"),
        "stage_id": mapping.as_int(task.get("stageId")),
        "priority": mapping.as_int(task.get("priority")) or 0,
        "deadline": task.get("deadline"),
        "overdue": views.is_overdue(task),
        "created_date": task.get("createdDate"),
        "closed_date": task.get("closedDate"),
        "parent_id": mapping.as_int(task.get("parentId")),
        "depth": depth,
        "responsible": {"id": mapping.as_int(task.get("responsibleId")),
                        "name": (task.get("responsible") or {}).get("name") or ""},
        "creator": {"id": mapping.as_int(task.get("createdBy")),
                    "name": (task.get("creator") or {}).get("name") or ""},
        "project": None if project is None else {
            "id": project.id, "name": project.name, "client": project.client_name,
            "b24_group_id": project.b24_group_id},
    }


def _context_json(ctx: miniapp.Context) -> dict[str, Any]:
    return {
        "chat_ref": ctx.chat_ref,
        "title": ctx.title or "чат без названия",
        "pinned": ctx.pinned,
        "task_id": ctx.task_id,
        "projects": [{"id": p.id, "name": p.name, "client": p.client_name,
                      "b24_group_id": p.b24_group_id} for p in ctx.projects],
    }


# ----------------------------------------------------------------- bootstrap
@router.get("/bootstrap")
async def bootstrap(request: Request,
                    ctx_param: Annotated[str | None, Query(alias="ctx")] = None
                    ) -> JSONResponse:
    """Всё, что нужно первому экрану. Не привязанный аккаунт — не ошибка, а экран."""
    try:
        actor = await actor_dep(request)
    except ApiError as exc:
        if exc.code != "not_linked":
            raise
        return JSONResponse({"state": "not_linked", "bot": exc.details.get("bot", ""),
                             "message": exc.message})

    try:
        ctx = await miniapp.resolve_context(actor, packed_ctx=ctx_param)
    except miniapp.Forbidden:
        ctx = None

    async with pool().acquire() as conn:
        portal = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1",
                                     actor.tenant_id)

    return JSONResponse({
        "state": "ok",
        "me": {"tg_user_id": actor.tg_user_id, "name": actor.init.full_name,
               "username": actor.init.username, "b24_user_id": actor.b24_user_id},
        "portal": portal or "",
        "context": _context_json(ctx) if ctx else None,
    })


@router.get("/contexts")
async def contexts(actor: ActorDep) -> JSONResponse:
    """Чаты на выбор — только для личного режима, когда контекст не задан ссылкой."""
    visible: set[int] | None = None
    try:
        client = await _client(actor)
        async with client:
            groups = await client.call("sonet_group.user.groups", {})
        visible = {int(g["GROUP_ID"]) for g in (groups or [])
                   if isinstance(g, dict) and str(g.get("GROUP_ID") or "").isdigit()}
    except (NeedsReauth, errors.B24Error) as exc:
        # Портал промолчал — покажем всё, что знаем сами, и скажем об этом честно.
        log.info("список групп недоступен: %s", exc)

    items = await miniapp.contexts_for(actor, visible)
    return JSONResponse({"items": items, "filtered": visible is not None})


# --------------------------------------------------------------------- список
def _list_filter(kind: str, b24_user_id: int, q: str) -> dict[str, Any]:
    flt: dict[str, Any] = {}
    if kind == "closed":
        flt["REAL_STATUS"] = mapping.STATUS_DONE
    else:
        flt["!=REAL_STATUS"] = mapping.STATUS_DONE
    if kind == "mine":
        flt["RESPONSIBLE_ID"] = b24_user_id
    if kind == "overdue":
        # Псевдостатус «просрочена» живёт в subStatus, а фильтровать по нему нельзя:
        # filter[STATUS]=3 не вернёт просроченную задачу «в работе» (§3.1).
        flt["<DEADLINE"] = datetime.now(UTC).isoformat(timespec="seconds")
    if q:
        flt["%TITLE"] = q[:100]
    return flt


@router.get("/tasks")
async def task_list(bundle: CtxDep,
                    kind: Annotated[str, Query(alias="filter")] = "all",
                    q: Annotated[str, Query()] = "",
                    project_id: Annotated[int | None, Query()] = None) -> JSONResponse:
    actor, ctx = bundle
    if kind not in ("all", "mine", "overdue", "closed"):
        raise ApiError(400, "validation", "Неизвестный фильтр.")

    projects = [p for p in ctx.projects if project_id in (None, p.id)]
    if not projects:
        raise ApiError(404, "not_found", "Проект не найден среди проектов этого чата.")
    group_ids = [p.b24_group_id for p in projects]

    flt = _list_filter(kind, actor.b24_user_id, q.strip())
    # GROUP_ID здесь не опция, а граница видимости: сервисной учётки с урезанным
    # доступом у нас нет, и область выборки задаёт только этот фильтр (И-3).
    flt["GROUP_ID"] = group_ids

    order = {"CLOSED_DATE": "desc"} if kind == "closed" else {"DEADLINE": "asc"}
    async with await _client(actor) as client:
        res = await client.call("tasks.task.list", {
            "filter": flt,
            "select": mapping.TASK_SELECT_LIST,
            "order": order,
        })
    raw = res.get("tasks", []) if isinstance(res, dict) else []
    items = [t for t in raw if isinstance(t, dict)][:LIST_LIMIT]

    ordered = views.order_by_hierarchy(items)
    return JSONResponse({
        "items": [_task_json(t, _project_of(ctx, t.get("groupId")), depth)
                  for t, depth in ordered],
        "total": len(items),
        "truncated": len(raw) > LIST_LIMIT,
        "stages": await _stages_json(actor.tenant_id, projects),
        "context": _context_json(ctx),
    })


async def _stages_json(tenant_id: int, projects: list[ProjectRef]) -> list[dict[str, Any]]:
    """Стадии канбана из нашего кэша: живой вызов на каждый список слишком дорог."""
    out: list[dict[str, Any]] = []
    for project in projects:
        for stage_id, title in await views.stages_of(tenant_id, project.id):
            out.append({"project_id": project.id, "id": stage_id, "title": title})
    return out


# -------------------------------------------------------------------- карточка
async def _authorized_task(actor: miniapp.Actor, ctx: miniapp.Context, client: Any,
                           task_id: int) -> tuple[dict[str, Any], ProjectRef]:
    """Инвариант И-3. Отказ всегда один и тот же, независимо от причины."""
    try:
        task = await task_service.read(client, task_id)
    except (errors.B24AccessDenied, errors.B24NotFound) as exc:
        # «Нет прав» и «нет такой задачи» обязаны отвечать одинаково, иначе перебор
        # номеров работает как оракул существования (И-3).
        raise ApiError(404, "not_found", TASK_NOT_FOUND) from exc

    project = await authorize_task_for_chat(
        actor.tenant_id, ctx.chat_ref, task_id,
        group_id_hint=mapping.as_int(task.get("groupId")))
    if project is None:
        raise ApiError(404, "not_found", TASK_NOT_FOUND)
    await remember_task(actor.tenant_id, project, task)
    return task, project


def _card_json(task: dict[str, Any], project: ProjectRef, portal: str,
               b24_user_id: int, stage_title: str = "") -> dict[str, Any]:
    card = _task_json(task, project)
    card.update({
        # Описание портал хранит в BBCode — человеку показываем текст.
        "description": bbcode_to_text(task.get("description") or ""),
        "accomplices": [mapping.as_int(x) for x in (task.get("accomplices") or [])],
        "auditors": [mapping.as_int(x) for x in (task.get("auditors") or [])],
        "tags": mapping.parse_tags(task.get("tags")),
        "allowed": sorted(mapping.allowed_actions(task)),
        "changed_date": task.get("changedDate"),
        # Своего названия стадии портал в задаче не отдаёт — только `stageId`.
        "stage_title": stage_title,
        # Сумма списаний по задаче в секундах; у задачи без списаний портал
        # отдаёт null, и это то же самое, что ноль.
        "time_spent": mapping.as_int(task.get("timeSpentInLogs")) or 0,
        "portal_url": views.portal_task_url(portal, mapping.as_int(task.get("id")) or 0,
                                            b24_user_id),
    })
    return card


async def _card_payload(actor: miniapp.Actor, task: dict[str, Any],
                        project: ProjectRef) -> dict[str, Any]:
    """Карточка со всем, что требует базы: домен портала и название стадии."""
    async with pool().acquire() as conn:
        portal = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1",
                                     actor.tenant_id)
    stage_title = await views.resolve_stage_title(actor.tenant_id, project,
                                                  task.get("stageId"))
    return _card_json(task, project, str(portal or ""), actor.b24_user_id, stage_title)


@router.get("/tasks/{task_id}")
async def task_card(task_id: int, bundle: CtxDep) -> JSONResponse:
    actor, ctx = bundle
    async with await _client(actor) as client:
        task, project = await _authorized_task(actor, ctx, client, task_id)
    return JSONResponse(await _card_payload(actor, task, project))


@router.post("/tasks/{task_id}/action")
async def task_action(task_id: int, bundle: CtxDep, body: JsonBody) -> JSONResponse:
    """Смена состояния — только спец-методами: они проводят бизнес-логику и права."""
    actor, ctx = bundle
    act = str(body.get("act") or "")
    method = ACTIONS.get(act)
    if method is None:
        raise ApiError(400, "validation", "Неизвестное действие.")

    async with await _client(actor) as client:
        await _authorized_task(actor, ctx, client, task_id)
        await miniapp_suppress(actor, task_id, act)
        await client.call(method, {"taskId": task_id})
        task, project = await _authorized_task(actor, ctx, client, task_id)
    await _audit(actor, AUDIT_ACTIONS[act], task_id, project, {"act": act})
    return JSONResponse(await _card_payload(actor, task, project))


async def miniapp_suppress(actor: miniapp.Actor, task_id: int, act: str) -> None:
    from b24bot.domain import events as b24_events

    status = ACTION_STATUS.get(act)
    if status is not None:
        await b24_events.suppress_task_echo(actor.tenant_id, task_id,
                                            actor.b24_user_id, status=status)


# Словарь действий аудита общий с ботом: одно и то же действие обязано называться
# одинаково, откуда бы его ни сделали, иначе журнал бесполезен для разбора.
AUDIT_ACTIONS = {"complete": "task.complete", "defer": "task.defer",
                 "start": "task.status.change", "pause": "task.status.change",
                 "renew": "task.status.change"}
AUDIT_FIELDS = {"responsible_id": "task.responsible.change",
                "deadline": "task.deadline.change",
                "priority": "task.priority.change"}


async def _audit(actor: miniapp.Actor, action: str, task_id: int,
                 project: ProjectRef | None, detail: dict[str, Any]) -> None:
    await audit.record(actor.tenant_id, action, actor_id=actor.b24_user_id,
                       actor_tg_id=actor.tg_user_id, target=f"task:{task_id}",
                       project_id=project.id if project else None,
                       detail={**detail, "source": "miniapp"})


@router.patch("/tasks/{task_id}")
async def task_patch(task_id: int, bundle: CtxDep, body: JsonBody) -> JSONResponse:
    actor, ctx = bundle
    try:
        patch = task_service.validate_patch(body)
    except task_service.Invalid as exc:
        raise ApiError(400, "validation", exc.message, {"field": exc.field}) from exc

    async with await _client(actor) as client:
        await _authorized_task(actor, ctx, client, task_id)
        task, missed = await task_service.apply_patch(
            client, actor.tenant_id, task_id, patch,
            actor_b24_user_id=actor.b24_user_id)
        project = await authorize_task_for_chat(
            actor.tenant_id, ctx.chat_ref, task_id,
            group_id_hint=mapping.as_int(task.get("groupId")))
    if project is None:
        raise ApiError(404, "not_found", TASK_NOT_FOUND)
    await remember_task(actor.tenant_id, project, task)
    for field in patch:
        await _audit(actor, AUDIT_FIELDS.get(field, "task.edit"), task_id, project,
                     {"field": field, "not_applied": missed})

    card = await _card_payload(actor, task, project)
    # Битрикс молча игнорирует то, что не смог применить. Молчать вслед за ним —
    # значит показать «сохранено» там, где ничего не сохранилось.
    card["not_applied"] = missed
    return JSONResponse(card)


# ------------------------------------------------------ подтверждение задач
def _approval_json(item: approvals.PendingItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "task_id": item.b24_task_id,
        "title": item.task_title,
        "project": {"id": item.project_id, "name": item.project_name,
                    "client": item.client_name},
        "requested_at": item.requested_at.isoformat(),
    }


@router.get("/approvals")
async def approvals_list(actor: ActorDep) -> JSONResponse:
    """Список задач на подтверждение — как список дел, а не задачи одного чата.

    Единственный эндпоинт мини-аппа без `CtxDep`: решение ответственного не
    привязано к тому, из какого чата открыто приложение, — так же как
    «Ожидают подтверждения» в боте собирает задачи по всему теннанту.
    """
    user_id = await approvals.user_id_of(actor.tg_user_id)
    if user_id is None:
        return JSONResponse({"items": [], "total": 0})
    items, total = await approvals.pending_for(actor.tenant_id, user_id)
    return JSONResponse({"items": [_approval_json(i) for i in items], "total": total})


@router.post("/approvals/{approval_id}/action")
async def approval_action(approval_id: int, actor: ActorDep, body: JsonBody) -> JSONResponse:
    decision_raw = str(body.get("decision") or "")
    if decision_raw not in ("confirm", "reject"):
        raise ApiError(400, "validation", "Неизвестное решение.")
    decision: approvals.Decision = "confirm" if decision_raw == "confirm" else "reject"

    result = await approvals.resolve(actor.tenant_id, approval_id, decision,
                                     actor.tg_user_id, source="miniapp")
    if result.outcome in ("confirmed", "rejected"):
        return JSONResponse({"outcome": result.outcome, "task_id": result.task_id,
                             "title": result.title, "stage": result.stage_title})
    if result.outcome == "already_done":
        raise ApiError(409, "already_done", "Решение по этой задаче уже принято.")
    if result.outcome == "needs_reauth":
        raise ApiError(403, "needs_reauth",
                       "Доступ к Битрикс24 истёк. Откройте приложение внутри портала "
                       "и привяжите Telegram заново.")
    if result.outcome == "b24_error":
        raise ApiError(502, "upstream_error", "Битрикс24 не ответил. Попробуйте ещё раз.")
    # forbidden, not_found — тот же однотипный отказ, что и у И-3: не раскрываем,
    # что именно не так с чужим или устаревшим идентификатором запроса.
    raise ApiError(404, "not_found", "Запрос на подтверждение не найден.")


# ---------------------------------------------------------------- комментарии
@router.get("/tasks/{task_id}/comments")
async def task_comments(task_id: int, bundle: CtxDep) -> JSONResponse:
    actor, ctx = bundle
    async with await _client(actor) as client:
        await _authorized_task(actor, ctx, client, task_id)
        items = await comments_service.read_discussion(client, task_id,
                                                       limit=COMMENTS_LIMIT)
    return JSONResponse({"items": items})


@router.post("/tasks/{task_id}/comments")
async def add_comment(task_id: int, bundle: CtxDep, body: JsonBody) -> JSONResponse:
    actor, ctx = bundle
    text = str(body.get("text") or "").strip()
    if not text:
        raise ApiError(400, "validation", "Пустой комментарий.")
    if len(text) > task_service.COMMENT_MAX:
        raise ApiError(400, "validation", "Комментарий слишком длинный.")

    async with await _client(actor) as client:
        await _authorized_task(actor, ctx, client, task_id)
        await comments_service.add(client, task_id, text, author=_author(actor),
                                   chat_title=ctx.title)
        await _audit(actor, "task.comment", task_id, None, {"length": len(text)})
        items = await comments_service.read_discussion(client, task_id,
                                                       limit=COMMENTS_LIMIT)
    return JSONResponse({"items": items})


def _author(actor: miniapp.Actor) -> str:
    name = actor.init.full_name
    return f"{name} (@{actor.init.username})" if actor.init.username else name


# ---------------------------------------------------------------- трудозатраты
@router.get("/tasks/{task_id}/timelog")
async def task_timelog(task_id: int, bundle: CtxDep) -> JSONResponse:
    """Списания по задаче: кто, сколько, когда и с каким комментарием.

    Полнота списка доказывается сходимостью с `TIME_SPENT_IN_LOGS` задачи, а не
    размером выборки: у метода портала нет ни фильтров, ни страниц.
    """
    actor, ctx = bundle
    async with await _client(actor) as client:
        task, _ = await _authorized_task(actor, ctx, client, task_id)
        total = mapping.as_int(task.get("timeSpentInLogs")) or 0
        entries = await timelog.for_task(client, task_id, total)
        names = await task_service.user_names(
            client, [e.user_id for e in entries.entries])
    return JSONResponse(_timelog_body(entries, names))


@router.post("/tasks/{task_id}/timelog")
async def add_timelog(task_id: int, bundle: CtxDep, body: JsonBody) -> JSONResponse:
    """Списать время. Длительность принимаем и числом, и человеческой строкой.

    Строка нужна, потому что форма мини-аппа и команда `/time` в чате обязаны
    понимать «1ч30м» одинаково: разбор один и тот же (`timelog.parse_duration`),
    иначе одно и то же написание давало бы в двух дверях разные минуты.
    """
    actor, ctx = bundle
    raw = body.get("seconds")
    try:
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            seconds = timelog.checked(int(raw))
        else:
            seconds = timelog.parse_duration(str(body.get("duration") or ""))
    except timelog.BadDuration as exc:
        raise ApiError(400, "validation", str(exc)) from exc

    comment = str(body.get("comment") or "").strip()[:timelog.COMMENT_MAX]
    started_at = task_service.parse_deadline(body.get("started_at")) \
        if body.get("started_at") else None

    async with await _client(actor) as client:
        _, project = await _authorized_task(actor, ctx, client, task_id)
        await timelog.add(client, task_id, seconds, comment=comment,
                          started_at=started_at)
        # Имя — из общей константы: у бота и здесь оно обязано быть одним.
        await _audit(actor, timelog.AUDIT_ACTION, task_id, project,
                     {"seconds": seconds})
        fresh = await task_service.read(client, task_id)
        total = mapping.as_int(fresh.get("timeSpentInLogs")) or 0
        entries = await timelog.for_task(client, task_id, total)
        names = await task_service.user_names(
            client, [e.user_id for e in entries.entries])
    return JSONResponse(_timelog_body(entries, names))


def _timelog_body(entries: timelog.TaskEntries,
                  names: dict[int, str]) -> dict[str, Any]:
    return {
        "total_seconds": entries.total_seconds,
        # False означает «часть списаний за окном выборки портала», а не «их нет».
        # Разница видна только здесь, и молчать о ней нельзя.
        "complete": entries.complete,
        "items": [{"id": e.id, "seconds": e.seconds,
                   "user_id": e.user_id,
                   "user_name": names.get(e.user_id, f"пользователь {e.user_id}"),
                   "at": e.at.isoformat() if e.at else None,
                   "comment": e.comment}
                  for e in entries.entries],
        "presets": [{"seconds": s, "label": timelog.preset_label(s)}
                    for s in timelog.PRESETS],
    }


# -------------------------------------------------------------------- создание
@router.post("/tasks")
async def create_task(bundle: CtxDep, body: JsonBody) -> JSONResponse:
    actor, ctx = bundle

    form_id = str(body.get("form_id") or "")
    if not FORM_ID_RE.match(form_id):
        raise ApiError(400, "validation", "Форма без идентификатора.")

    project = next((p for p in ctx.projects if p.id == body.get("project_id")), None)
    if project is None:
        project = ctx.projects[0] if len(ctx.projects) == 1 else None
    if project is None:
        raise ApiError(400, "validation", "Не выбран проект.")

    # Задача по опроснику: заголовок, тело и поля собирает `survey.assemble` —
    # ровно тот же код, что и в боте. Иначе один и тот же набор вопросов давал бы
    # в чате и в приложении две разные задачи.
    survey_fields: dict[str, Any] = {}
    assembled = await _assemble_survey(actor, body)
    if assembled is not None:
        title = assembled.title
        survey_fields = assembled.fields
    else:
        title = " ".join(str(body.get("title") or "").split())
    if len(title) < 3:
        raise ApiError(400, "validation", "Заголовок слишком короткий.")

    try:
        deadline = task_service.parse_deadline(body.get("deadline"))
        priority = (None if body.get("priority") is None
                    else task_service.validate_patch(
                        {"priority": body["priority"]})["priority"])
    except task_service.Invalid as exc:
        raise ApiError(400, "validation", exc.message, {"field": exc.field}) from exc

    description = _description(body, actor, ctx, project,
                               extra=assembled.description if assembled else "")
    draft = task_create.Draft(
        title=title[:task_service.TITLE_MAX], description=description,
        idem_key=f"tgapp-{ctx.chat_ref}-{form_id}", source_message_id=None,
        fields=survey_fields,
        responsible_id=mapping.as_int(body.get("responsible_id")),
        deadline=deadline or None, priority=priority,
        stage_id=mapping.as_int(body.get("stage_id")),
        accomplices=_ids(body.get("accomplices")),
        auditors=_ids(body.get("auditors")))

    async with await _client(actor) as client:
        task, created = await task_create.create(client, actor.tenant_id, project,
                                                 draft, actor.b24_user_id)
        fresh = await task_service.read(client, mapping.as_int(task.get("id")) or 0)

    await remember_task(actor.tenant_id, project, fresh or task)
    if created:
        await _audit(actor, "task.create", mapping.as_int(task.get("id")) or 0, project,
                     {"title": title[:80]})
    async with pool().acquire() as conn:
        portal = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1",
                                     actor.tenant_id)
    card = _card_json(fresh or task, project, str(portal or ""), actor.b24_user_id)
    card["created"] = created
    return JSONResponse(card, status_code=201 if created else 200)


async def _assemble_survey(actor: miniapp.Actor,
                           body: dict[str, Any]) -> survey.Assembled | None:
    """Ответы опросника → заголовок, тело и поля задачи.

    Разбирает `survey.assemble`, а не мы: там же лежит правило «ответ, который
    поле не приняло, уходит в тело, а не пропадает». Продублировать его здесь
    значило бы однажды потерять написанное человеком в одном из двух интерфейсов.
    """
    template_id = mapping.as_int(body.get("survey_template_id"))
    if not template_id:
        return None
    raw = body.get("answers")
    if not isinstance(raw, dict):
        raise ApiError(400, "validation", "Ответы опросника не пришли.")

    items = await survey.questions(actor.tenant_id, template_id)
    if not items:
        raise ApiError(404, "not_found", "Набор вопросов не найден.")

    answers = {str(k): str(v) for k, v in raw.items() if v is not None}
    missing = [q.text for q in items
               if q.required and not (answers.get(q.code) or "").strip()]
    if missing:
        raise ApiError(400, "validation",
                       f"Не отвечено обязательное: {missing[0]}",
                       {"questions": missing})
    return survey.assemble(items, answers)


def _description(body: dict[str, Any], actor: miniapp.Actor, ctx: miniapp.Context,
                 project: ProjectRef, *, extra: str = "") -> str:
    """Описание плюс блок источника — тот же, что у задач из чата (И-6)."""
    from b24bot.core.text import esc_bbcode

    del project
    text = esc_bbcode(str(body.get("description") or "").strip())
    meta = ["[b]— Источник —[/b]",
            f"Telegram: мини-апп, чат «{esc_bbcode(ctx.title)}»",
            f"Автор: {esc_bbcode(_author(actor))}"]
    # `extra` уже прошло esc_bbcode внутри survey.assemble — второй проход
    # превратил бы разметку заголовков ответов в текст.
    parts = [p for p in (extra, text) if p]
    parts.append("\n".join(meta))
    return "\n\n".join(parts)[:task_service.DESCRIPTION_MAX]


def _ids(value: Any) -> list[int] | None:
    if not isinstance(value, list):
        return None
    out = [mapping.as_int(v) for v in value[:20]]
    return [v for v in out if v] or None


# ------------------------------------------------------------- справочники
def _project_by_id(ctx: miniapp.Context, project_id: int) -> ProjectRef:
    project = next((p for p in ctx.projects if p.id == project_id), None)
    if project is None:
        raise ApiError(404, "not_found", "Проект не найден среди проектов этого чата.")
    return project


@router.get("/projects/{project_id}/members")
async def project_members(project_id: int, bundle: CtxDep) -> JSONResponse:
    actor, ctx = bundle
    project = _project_by_id(ctx, project_id)
    async with await _client(actor) as client:
        items = await task_service.group_members(client, project.b24_group_id)
    return JSONResponse({"items": items})


@router.get("/projects/{project_id}/stages")
async def project_stages(project_id: int, bundle: CtxDep) -> JSONResponse:
    actor, ctx = bundle
    project = _project_by_id(ctx, project_id)
    async with await _client(actor) as client:
        items = await task_service.stages_of_group(client, project.b24_group_id)
    return JSONResponse({"items": items})


# ------------------------------------------------- трансляция отказов портала
def portal_failure(exc: Exception) -> ApiError:
    """Один перевод ошибок Битрикса в наш контракт — для всех путей сразу.

    Подключается обработчиком уровня приложения (api/main.py): часть вызовов идёт
    не отсюда напрямую, а из общего кода (`domain/tasks`, `bot/comments`), и ловить
    их в каждом обработчике — способ однажды забыть.
    """
    if isinstance(exc, NeedsReauth):
        return ApiError(403, "needs_reauth",
                        "Доступ к Битрикс24 истёк. Откройте приложение внутри портала "
                        "и привяжите Telegram заново.")
    if isinstance(exc, errors.B24AccessDenied):
        return ApiError(403, "b24_forbidden",
                        exc.description or "Битрикс24 отказал в доступе.")
    if isinstance(exc, errors.B24NotFound):
        return ApiError(404, "not_found", TASK_NOT_FOUND)
    if isinstance(exc, errors.B24QueryLimit | errors.B24OperatingLimit):
        return ApiError(429, "rate_limited",
                        "Битрикс24 ограничил частоту запросов. Повторите через минуту.")
    return ApiError(502, "upstream_error", "Битрикс24 не ответил.")


# =====================================================================
# Паритет с ботом
#
# Всё, что бот умеет командами и кнопками, обязан уметь и мини-апп: иначе
# человек, привыкший к одному интерфейсу, упирается в другом в отсутствующую
# функцию и не понимает, потерялась она или её не было.
#
# Логика везде ПЕРЕИСПОЛЬЗУЕТСЯ, а не переписывается: трудозатраты считает
# `domain/timesheet`, сводку — `bot/views`, опросник разбирает `bot/survey`.
# Разъедься реализации — одно и то же считалось бы в боте и в приложении
# по-разному, а это худший вид расхождения: оба ответа выглядят уверенно.
# =====================================================================


# ------------------------------------------------------------------ сводка
@router.get("/summary")
async def summary(bundle: CtxDep) -> JSONResponse:
    """Сводка по стадиям канбана — то же, что «📊 Сводка» в боте.

    Отдаём структурой, а не готовым текстом: бот рисует строки, приложение —
    полосы, и разметка HTML для Telegram здесь была бы мусором. Числа при этом
    считаются тем же кодом и по тем же правилам.
    """
    actor, ctx = bundle
    async with await _client(actor) as client:
        tasks = await views.fetch_open(client, [p.b24_group_id for p in ctx.projects])

    projects: list[dict[str, Any]] = []
    for project in ctx.projects:
        mine = [t for t in tasks
                if mapping.as_int(t.get("groupId")) == project.b24_group_id]
        stages = await views.stages_of(actor.tenant_id, project.id)

        # Незнакомая стадия почти всегда значит одно: колонку завели в Битриксе
        # после нашей последней синхронизации. Ждать суточного прохода нельзя —
        # человек смотрит на сводку сейчас, и её задачи числились бы «вне канбана».
        if _unknown_stage_ids(mine, stages) and await sync.ensure_fresh(
                actor.tenant_id, project.id, project.b24_group_id):
            stages = await views.stages_of(actor.tenant_id, project.id)

        rows: list[dict[str, Any]] = []
        counted = 0
        for stage_id, title in stages:
            n = sum(1 for t in mine if mapping.as_int(t.get("stageId")) == stage_id)
            counted += n
            if n:
                rows.append({"id": stage_id, "title": title, "count": n})

        # Две разные вещи, и путать их нельзя: «вне канбана» — нормальное
        # состояние задачи, «стадия не опознана» — наш разлад с порталом.
        outside = sum(1 for t in mine if not mapping.as_int(t.get("stageId")))
        unresolved = len(mine) - counted - outside
        projects.append({
            "id": project.id, "name": project.name, "client": project.client_name,
            "open": len(mine), "stages": rows,
            "outside": outside, "outside_title": views.OUTSIDE_KANBAN,
            "unresolved": max(0, unresolved), "unresolved_title": views.UNKNOWN_STAGE,
            "overdue": sum(1 for t in mine if views.is_overdue(t)),
            "mine": sum(1 for t in mine
                        if mapping.as_int(t.get("responsibleId")) == actor.b24_user_id),
        })

    return JSONResponse({
        "projects": projects,
        "open": len(tasks),
        "overdue": sum(1 for t in tasks if views.is_overdue(t)),
        "mine": sum(1 for t in tasks
                    if mapping.as_int(t.get("responsibleId")) == actor.b24_user_id),
    })


def _unknown_stage_ids(tasks: list[dict[str, Any]],
                       stages: list[tuple[int, str]]) -> set[int]:
    """Стадии задач, которых нет в справочнике. Ноль не в счёт: это «вне канбана»."""
    known = {stage_id for stage_id, _ in stages}
    seen = {mapping.as_int(t.get("stageId")) or 0 for t in tasks}
    return {s for s in seen if s and s not in known}


# ------------------------------------------------------------ трудозатраты
@router.get("/timesheet/months")
async def timesheet_months(actor: ActorDep) -> JSONResponse:
    """Месяцы на выбор. Список всегда одной длины — как кнопки в боте."""
    del actor
    items = [{"value": f"{year}-{month:02d}", "title": timesheet.month_title(year, month)}
             for year, month in timesheet.months_back(datetime.now(UTC).date())]
    return JSONResponse({"items": items, "current": items[0]["value"] if items else ""})


@router.get("/timesheet")
async def timesheet_report(bundle: CtxDep,
                           month: Annotated[str, Query()] = "") -> JSONResponse:
    """Свод трудозатрат за месяц по проектам чата.

    Ходим личным токеном человека: видно ровно то, что видно ему самому.
    Границу задаёт список групп чата — тот же, что и у всех выборок (И-3).
    """
    actor, ctx = bundle
    parsed = timesheet.parse_month(month) if month else None
    if parsed is None:
        today = datetime.now(UTC).date()
        parsed = (today.year, today.month)
    year, month_number = parsed

    async with await _client(actor) as client:
        snap = await timesheet.snapshot(client, actor.tenant_id,
                                        [p.b24_group_id for p in ctx.projects])

    # Названия стадий — из справочника проектов: в своде их может быть несколько,
    # и колонки разных проектов встают в одном порядке с их канбаном.
    stage_titles: dict[int, str] = {}
    for project in ctx.projects:
        stage_titles.update(dict(await views.stages_of(actor.tenant_id, project.id)))

    report = timesheet.aggregate(
        snap.entries, snap.tasks, stage_titles, year=year, month=month_number,
        complete=snap.complete, seen=snap.seen, total_on_portal=snap.total_on_portal,
        tag=snap.tag)

    def bucket(b: timesheet.Bucket) -> dict[str, Any]:
        return {"title": b.title, "seconds": b.seconds, "tasks": len(b.tasks)}

    return JSONResponse({
        "month": f"{report.year}-{report.month:02d}",
        "title": report.title,
        "by_status": [bucket(b) for b in report.by_status],
        "by_stage": [bucket(b) for b in report.by_stage],
        "total_seconds": report.total_seconds,
        "task_count": report.task_count,
        "entry_count": report.entry_count,
        # Постраничность `task.elapseditem.getlist` на портале сломана, и полноту
        # выборки приходится доказывать сверкой с `total` из конверта. Не сошлось —
        # человеку говорится прямо, что сумма снизу (docs/00-portal-facts.md §5.2).
        "complete": report.complete,
        "seen": report.seen,
        "total_on_portal": report.total_on_portal,
        # Тег поддержки задан — рядом с итогом по поддержке стоит итог по всем
        # задачам проектов. Без второй суммы падение первой читается как падение
        # объёма работ вообще, а означает лишь то, что работу завели мимо бота.
        "split_by_tag": report.split_by_tag,
        "support_tag": report.support_tag,
        "all_seconds": report.all_seconds,
        "all_task_count": report.all_task_count,
        "all_entry_count": report.all_entry_count,
        "projects": [{"id": p.id, "name": p.name, "client": p.client_name}
                     for p in ctx.projects],
    })


# --------------------------------------------------------------- опросник
@router.get("/surveys")
async def survey_templates(bundle: CtxDep) -> JSONResponse:
    """Наборы вопросов — те же, что предлагает `/ask` в боте."""
    actor, _ = bundle
    items = await survey.categories(actor.tenant_id)
    return JSONResponse({"items": [{"id": tid, "title": title} for tid, title in items]})


@router.get("/surveys/{template_id}")
async def survey_form(template_id: int, bundle: CtxDep) -> JSONResponse:
    """Вопросы набора — все сразу.

    В этом и разница с ботом, и весь смысл: в чате вопросы задаются по одному,
    потому что диалог линеен, а на экране их видно списком — можно вернуться,
    исправить и понять, сколько осталось, ещё до того как начал отвечать.
    """
    actor, _ = bundle
    items = await survey.questions(actor.tenant_id, template_id)
    if not items:
        raise ApiError(404, "not_found", "Набор вопросов не найден.")
    return JSONResponse({"items": [
        {"code": q.code, "text": q.text, "required": q.required, "kind": q.kind,
         "options": [{"value": str(o.get("value") or ""),
                      "label": str(o.get("label") or o.get("value") or "")}
                     for o in q.options],
         # Привязку к полю показываем: человек вправе знать, что его ответ
         # станет сроком задачи, а не строкой в описании.
         "field": q.b24_field or ""}
        for q in items]})


# --------------------------------------------------------------- вложения
FILES_PER_REQUEST = 10


@router.post("/tasks/{task_id}/files")
async def upload_files(task_id: int, bundle: CtxDep,
                       files: Annotated[list[UploadFile], File()]) -> JSONResponse:
    """Прикрепить файлы к задаче — то же, что послать файл боту с `/comment`.

    Отличается только источник: в чате файл берётся из Telegram по `file_id`,
    здесь его присылает браузер. Предел, чёрный список расширений и запись в
    `tg_attachments` — общие с ботом, чтобы «20 МБ» значило одно и то же в
    обоих интерфейсах.
    """
    actor, ctx = bundle
    rejected: list[str] = []

    # Отсев ДО обращения к порталу: «.exe» и файл на 30 МБ отказываются одинаково
    # и без хранилища, а искать папку ради того, чтобы ничего в неё не положить, —
    # лишний поход к порталу против его же лимита частоты.
    accepted: list[tuple[str, bytes]] = []
    total = 0
    for item in files[:FILES_PER_REQUEST]:
        name = safe_filename(item.filename or "файл")
        if tg_files.is_blocked(name):
            rejected.append(f"«{name}» — этот тип файла не переносим")
            continue
        content = await item.read()
        if len(content) > tg_files.MAX_SIZE:
            rejected.append(f"«{name}» больше 20 МБ")
            continue
        total += len(content)
        if total > tg_files.TOTAL_PER_TASK:
            rejected.append(f"«{name}» — превышен общий объём на задачу")
            break
        accepted.append((name, content))

    async with await _client(actor) as client:
        task, project = await _authorized_task(actor, ctx, client, task_id)

        uploaded: list[int] = []
        names: list[str] = []

        if accepted:
            folder = await disk.group_folder(client, project.b24_group_id)
            if folder is None:
                folder = await disk.user_folder(client, actor.b24_user_id)
            if folder is None:
                raise ApiError(502, "upstream_error",
                               "В Битрикс24 не нашлось хранилища для файлов.")

            for name, content in accepted:
                # Ключ идемпотентности — sha256 содержимого: `file_id` Telegram
                # здесь взять неоткуда, а повторная отправка той же формы после
                # таймаута не должна класть в задачу второй такой же файл (И-10).
                digest = hashlib.sha256(content).hexdigest()
                idem = f"tgapp-file-{task_id}"
                async with pool().acquire() as conn:
                    done = await conn.fetchval(
                        "SELECT b24_file_id FROM tg_attachments WHERE tenant_id = $1 "
                        "AND idem_key = $2 AND tg_file_id = $3 AND state = 'uploaded'",
                        actor.tenant_id, idem, digest)
                # `as_int`, а не `int()`: строка идемпотентности — подсказка,
                # а не источник правды. Мусор в ней должен привести к повторной
                # заливке, а не уронить весь запрос на полпути.
                known = mapping.as_int(done)
                if known:
                    uploaded.append(known)
                    names.append(name)
                    continue

                try:
                    result = await disk.upload(client, folder, name, content)
                except errors.B24Error as exc:
                    log.warning("файл %s не загрузился: %s", name, exc)
                    rejected.append(f"«{name}» не загрузился в Битрикс24")
                    continue

                file_id = mapping.as_int(result.get("ID")) or 0
                if not file_id:
                    rejected.append(f"«{name}» не загрузился в Битрикс24")
                    continue

                uploaded.append(file_id)
                names.append(name)
                async with pool().acquire() as conn:
                    await conn.execute(
                        "INSERT INTO tg_attachments (tenant_id, idem_key, tg_file_id, "
                        "file_name, size_bytes, state, b24_file_id) "
                        "VALUES ($1,$2,$3,$4,$5,'uploaded',$6) "
                        "ON CONFLICT (tenant_id, idem_key, tg_file_id) DO UPDATE "
                        "SET state = 'uploaded', b24_file_id = EXCLUDED.b24_file_id",
                        actor.tenant_id, idem, digest, name, len(content), file_id)

        if uploaded:
            await disk.attach_to_task(client, task_id, uploaded)
            await _audit(actor, "task.attach", task_id, project,
                         {"files": len(uploaded)})

        fresh = await task_service.read(client, task_id) or task
        payload = await _card_payload(actor, fresh, project)

    payload["attached"] = len(uploaded)
    payload["attached_names"] = names
    # Отказы не молчат: файл, который не уехал, человек считает уехавшим.
    payload["rejected"] = rejected
    return JSONResponse(payload)


# ----------------------------------------------------------------- кто я
@router.get("/me")
async def whoami(actor: ActorDep) -> JSONResponse:
    """`/whoami` бота: теннант, портал, роль, привязка.

    Единственное место, где человек может проверить, ЧЕМ он в системе является,
    когда что-то не работает. В боте оно есть — значит обязано быть и здесь.
    """
    async with pool().acquire() as conn:
        portal = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1",
                                     actor.tenant_id)
        name = await conn.fetchval("SELECT name FROM tenants WHERE id = $1",
                                   actor.tenant_id)
        role = await conn.fetchval(
            "SELECT role FROM tenant_users WHERE tenant_id = $1 AND b24_user_id = $2",
            actor.tenant_id, actor.b24_user_id)
    return JSONResponse({
        "tenant": {"id": actor.tenant_id, "name": str(name or "")},
        "portal": str(portal or ""),
        "b24_user_id": actor.b24_user_id,
        "tg_user_id": actor.tg_user_id,
        "name": actor.init.full_name,
        "username": actor.init.username or "",
        "role": role or "user",
    })
