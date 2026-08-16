"""Обработчики Битрикс24: установка приложения, placement, события.

Это скелет под SPIKE A. Задача — принять реальные запросы портала, зафиксировать
их структуру и получить первый живой per-user токен.

Инварианты, действующие уже здесь:
  И-4  хосты только из allowlist; SERVER_ENDPOINT из тела запроса ИГНОРИРУЕТСЯ
  И-7  токены только в шифрованных колонках
  И-8  APPLICATION_TOKEN — идентификатор портала, не доказательство подлинности
"""
from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from b24bot.api import app_ui
from b24bot.api import ui_kit as ui
from b24bot.core.config import get_settings, is_trusted_portal_domain
from b24bot.core.text import esc_html
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.domain import events as event_queue

log = logging.getLogger(__name__)
router = APIRouter(prefix="/b24", tags=["bitrix24"])

SECRET_FIELDS = {"AUTH_ID", "REFRESH_ID", "APPLICATION_TOKEN", "access_token",
                 "refresh_token", "application_token", "APP_SID"}


# ----------------------------------------------------------------- разбор входа
def _flatten(form: Mapping[str, Any]) -> dict[str, str]:
    """Битрикс шлёт и плоские поля, и auth[...] — приводим к одному виду."""
    out: dict[str, str] = {}
    for k, v in form.items():
        if isinstance(v, str):
            out[k] = v
    return out


def normalize(payload: dict[str, str]) -> dict[str, str | None]:
    """Два формата: placement-POST (AUTH_ID/...) и событие (auth[access_token]/...)."""
    g = payload.get
    return {
        "member_id": g("member_id") or g("auth[member_id]"),
        "domain": g("DOMAIN") or g("auth[domain]"),
        "access_token": g("AUTH_ID") or g("auth[access_token]"),
        "refresh_token": g("REFRESH_ID") or g("auth[refresh_token]"),
        "app_token": g("APPLICATION_TOKEN") or g("auth[application_token]"),
        "placement": g("PLACEMENT"),
        "placement_options": g("PLACEMENT_OPTIONS"),
        "status": g("status") or g("auth[status]"),
        "event": g("event"),
        "scope": g("APPLICATION_SCOPE") or g("auth[scope]"),
    }


def shape_for_log(payload: dict[str, str]) -> dict[str, str]:
    """Структура payload без секретов — то, ради чего затевался SPIKE A."""
    out = {}
    for k, v in payload.items():
        base = k.split("[")[-1].rstrip("]")
        out[k] = box.redact(v) if (k in SECRET_FIELDS or base in SECRET_FIELDS) else v[:200]
    return out


# ------------------------------------------------------------- вызовы в портал
async def b24_call(domain: str, method: str, access_token: str,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
    """REST-вызов. client_endpoint собираем САМИ из проверенного домена (И-4)."""
    if not is_trusted_portal_domain(domain):
        raise ValueError(f"домен портала не прошёл проверку: {domain!r}")
    url = f"https://{domain}/rest/{method}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, data={**(params or {}), "auth": access_token})
        payload: dict[str, Any] = r.json()
        return payload


# ------------------------------------------------------------------ сохранение
async def upsert_tenant(n: dict[str, str | None], granted_scope: str | None) -> int:
    domain = n["domain"] or ""
    if not is_trusted_portal_domain(domain):
        raise ValueError(f"недоверенный домен портала: {domain!r}")

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tenants (slug, name, b24_member_id, b24_domain, granted_scope,
                                 install_state)
            VALUES ($1, $2, $3, $4, $5, 'installed')
            ON CONFLICT (b24_member_id) DO UPDATE
              SET b24_domain = EXCLUDED.b24_domain,
                  granted_scope = COALESCE(EXCLUDED.granted_scope, tenants.granted_scope),
                  updated_at = now()
            RETURNING id
            """,
            domain.split(".")[0], domain, n["member_id"], domain,
            (granted_scope or "").split(",") if granted_scope else None,
        )
        tenant_id = int(row["id"])

        if n["app_token"]:
            enc = box.encrypt(n["app_token"],
                              box.aad("tenants", "b24_app_token", tenant_id, n["member_id"] or ""))
            await conn.execute(
                "UPDATE tenants SET b24_app_token = $1, b24_app_token_kid = $2 WHERE id = $3",
                enc, box.kid_of(enc), tenant_id)
    return tenant_id


async def store_user_token(tenant_id: int, member_id: str, b24_user_id: int,
                           access_token: str, refresh_token: str, role: str) -> None:
    a_enc = box.encrypt(access_token,
                        box.aad("b24_user_tokens", "access_token", tenant_id, b24_user_id))
    r_enc = box.encrypt(refresh_token,
                        box.aad("b24_user_tokens", "refresh_token", tenant_id, b24_user_id))
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO b24_user_tokens (tenant_id, b24_user_id, role, access_token,
                                         refresh_token, enc_kid, expires_at, state,
                                         last_refresh_at)
            VALUES ($1, $2, $3, $4, $5, $6, now() + interval '1 hour', 'active', now())
            ON CONFLICT (tenant_id, b24_user_id) DO UPDATE
              SET access_token = EXCLUDED.access_token,
                  refresh_token = EXCLUDED.refresh_token,
                  enc_kid = EXCLUDED.enc_kid,
                  expires_at = EXCLUDED.expires_at,
                  state = 'active',
                  token_version = b24_user_tokens.token_version + 1,
                  last_refresh_at = now()
            """,
            tenant_id, b24_user_id, role, a_enc, r_enc, box.kid_of(a_enc))


async def log_payload(kind: str, member_id: str | None, payload: dict[str, str]) -> None:
    if not get_settings().spike_log_payloads:
        return
    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO b24_payload_log (kind, member_id, shape) VALUES ($1, $2, $3)",
            kind, member_id, json.dumps(shape_for_log(payload), ensure_ascii=False))


# ---------------------------------------------------------------------- страницы
def _notice(kind: str, title: str, text: str, portal_domain: str | None, *,
            extra_html: str = "") -> HTMLResponse:
    """Служебный экран установки или отказа.

    Раньше здесь была своя маленькая копия вёрстки со своими хексами, и экраны
    установки выглядели чужими рядом с самим приложением. Теперь это тот же
    дизайн: человек попадает в приложение с первого же экрана, а не после него.

    Причина отказа намеренно не уточняется: сообщения «нет такого портала» и
    «токен не совпал» вместе работают оракулом для подбора.
    """
    icon_name = {"ok": "check-circle", "warn": "alert", "err": "x-circle"}.get(kind, "info")
    body = (ui.panel("", ui.empty(title, text, icon_name=icon_name)) + extra_html)
    return app_ui.page(body, portal_domain)


# ------------------------------------------------------------------ /b24/install
@router.post("/install")
@router.get("/install")
async def install(request: Request) -> Response:
    form = _flatten(dict(await request.form())) if request.method == "POST" else {}
    payload = {**dict(request.query_params), **form}
    n = normalize(payload)
    await log_payload("install", n["member_id"], payload)

    if not n["member_id"] or not n["domain"]:
        log.warning("install без member_id/domain: %s", shape_for_log(payload))
        return _notice("err", "Установка не завершена",
                       "Портал не передал идентификатор. Откройте установку из "
                       "интерфейса Битрикс24, а не по прямой ссылке.", None)

    tenant_id = await upsert_tenant(n, n["scope"])

    installer_id = None
    if n["access_token"] and n["refresh_token"]:
        me = await b24_call(n["domain"], "user.current", n["access_token"])
        installer_id = (me.get("result") or {}).get("ID")
        if installer_id:
            await store_user_token(tenant_id, n["member_id"], int(installer_id),
                                   n["access_token"], n["refresh_token"], "service_admin")

    async with pool().acquire() as conn:
        await conn.execute("UPDATE tenants SET install_state = 'finished' WHERE id = $1",
                           tenant_id)

    log.info("установка завершена: tenant=%s domain=%s installer=%s",
             tenant_id, n["domain"], installer_id)

    # installFinish обязателен: без него приложение считается неустановленным —
    # виджеты не показываются, события не приходят вообще (docs/50 §2.1).
    details = (ui.panel("Что дальше", ui.field(
        "Портал", f"<code>{esc_html(n['domain'])}</code>")
        + ui.field("Установил", f"<code>пользователь {esc_html(installer_id or '?')}</code>")
        + ui.hint("Откройте «Поддержка в Telegram» в левом меню портала: там "
                  "мастер настройки проведёт по трём шагам — бот, чат, проект."),
        icon_name="check-circle")
        + "<script>BX24.init(function(){ BX24.installFinish(); });</script>")

    return _notice("ok", "Приложение установлено",
                   "Интеграция зарегистрирована на портале. Осталось подключить "
                   "Telegram-бота и привязать чаты к проектам.",
                   n["domain"], extra_html=details)


# ---------------------------------------------------------------- /b24/placement
@router.post("/placement")
async def placement(request: Request) -> Response:
    form = _flatten(dict(await request.form()))
    payload = {**dict(request.query_params), **form}
    n = normalize(payload)
    await log_payload("placement", n["member_id"], payload)

    if not n["member_id"] or not is_trusted_portal_domain(n["domain"] or ""):
        return _notice("err", "Доступ не подтверждён",
                       "Откройте приложение из интерфейса Битрикс24.", None)

    async with pool().acquire() as conn:
        t = await conn.fetchrow(
            "SELECT id, b24_app_token, b24_domain, status FROM tenants WHERE b24_member_id = $1",
            n["member_id"])
    if not t:
        return _notice("warn", "Портал не подключён",
                       "Сначала установите приложение на портале — тогда этот "
                       "экран откроется сам.", n["domain"])

    # И-8: сверка APPLICATION_TOKEN подтверждает, что запрос от известного портала,
    # но НЕ доказывает, что его прислал Битрикс. Деструктивных действий здесь нет.
    #
    # Если токен у нас сохранён — он ОБЯЗАТЕЛЕН в запросе. Условие «сверяем, только
    # когда обе стороны непусты» означало бы, что запрос без APPLICATION_TOKEN
    # проверку просто обходит.
    if t["b24_app_token"]:
        expected = box.decrypt(t["b24_app_token"],
                               box.aad("tenants", "b24_app_token", t["id"], n["member_id"]))
        if not n["app_token"] or not hmac.compare_digest(expected, n["app_token"]):
            log.warning("placement: APPLICATION_TOKEN отсутствует или не совпал, "
                        "member_id=%s", n["member_id"])
            return _notice("err", "Доступ не подтверждён",
                           "Откройте приложение из интерфейса Битрикс24.",
                           n["domain"])

    b24_user_id: int | None = None
    is_portal_admin = False
    if n["access_token"] and n["refresh_token"]:
        me = await b24_call(t["b24_domain"], "user.current", n["access_token"])
        raw_id = (me.get("result") or {}).get("ID")
        if raw_id:
            b24_user_id = int(raw_id)
            await store_user_token(t["id"], n["member_id"], b24_user_id,
                                   n["access_token"], n["refresh_token"], "user")
            is_portal_admin = await _is_portal_admin(t["b24_domain"], n["access_token"])
            if is_portal_admin:
                # Первого админа теннанта назначить некому. Права администратора
                # портала мы не выдаём, а спрашиваем у Битрикса (user.admin), и
                # подтягиваем роль при каждом входе. Обратного действия нет:
                # снятие прав в портале роль не отбирает — это делается явно.
                from b24bot.domain import access, audit
                if await access.promote_portal_admin(t["id"], b24_user_id):
                    await audit.record(t["id"], "role.grant", actor_kind="system",
                                       actor_id=b24_user_id,
                                       target=f"b24_user:{b24_user_id}",
                                       detail={"причина": "администратор портала"})

    if b24_user_id is None:
        return _notice("warn", "Не удалось определить пользователя",
                       "Портал не прислал ваш токен. Закройте приложение и "
                       "откройте его из интерфейса Битрикс24 заново.",
                       t["b24_domain"])

    tenant = await _tenant_row(t["id"])
    async with pool().acquire() as conn:
        session = await app_ui.issue_session(conn, t["id"], b24_user_id, is_portal_admin)

    body = await app_ui.render_home(tenant, b24_user_id, is_portal_admin, session)
    return app_ui.page(body, t["b24_domain"])


async def _tenant_row(tenant_id: int) -> Any:
    async with pool().acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, b24_domain, install_state, granted_scope FROM tenants WHERE id = $1",
            tenant_id)


async def _is_portal_admin(domain: str, access_token: str) -> bool:
    """Права администратора портала спрашиваем у самого Битрикса.

    Своих ролей у нас пока нет, а решать, кому можно вводить токен бота, надо уже
    сейчас. Метод user.admin отвечает про ТЕКУЩЕГО пользователя токена, подделать
    ответ нельзя — запрос идёт с нашего сервера на портал.
    """
    try:
        res = await b24_call(domain, "user.admin", access_token)
    except Exception as exc:
        log.warning("user.admin недоступен: %s", str(exc)[:120])
        return False
    return bool(res.get("result"))


# ------------------------------------------------------------------- /b24/events
@router.post("/events")
async def events(request: Request) -> Response:
    form = _flatten(dict(await request.form()))
    payload = {**dict(request.query_params), **form}
    n = normalize(payload)
    await log_payload(f"event:{n['event'] or '?'}", n["member_id"], payload)

    # Отвечаем быстро и обрабатываем асинхронно: Битрикс не должен ждать, пока мы
    # сходим в портал за деталями задачи.
    tenant_id = await _tenant_by_member(n["member_id"], n["app_token"])
    if tenant_id is None:
        log.warning("событие от неизвестного или неподтверждённого портала")
        return JSONResponse({"ok": True})

    event_name = str(n["event"] or "").upper()
    task_id = _task_id_of(payload)
    actor = payload.get("auth[user_id]")
    dedup = (f"{event_name}:{task_id}:{payload.get('data[FIELDS_AFTER][MESSAGE_ID]') or ''}"
             f":{payload.get('ts') or ''}")

    accepted = await event_queue.ingest(
        tenant_id, event_name, task_id,
        int(actor) if actor and str(actor).isdigit() else None, dedup)
    log.info("событие %s задача=%s принято=%s", event_name, task_id, accepted)
    return JSONResponse({"ok": True})


def _task_id_of(payload: dict[str, str]) -> int | None:
    """ID задачи в событии лежит по-разному: у комментариев это TASK_ID."""
    for key in ("data[FIELDS_AFTER][TASK_ID]", "data[FIELDS_AFTER][ID]",
                "data[FIELDS_BEFORE][ID]"):
        value = payload.get(key)
        if value and str(value).isdigit():
            return int(value)
    return None


async def _tenant_by_member(member_id: str | None, app_token: str | None) -> int | None:
    """Опознание портала.

    И-8: APPLICATION_TOKEN — идентификатор, а НЕ доказательство подлинности, его видит
    любой сотрудник портала. Поэтому дальше по событию не делается ни одного
    деструктивного действия: обработчик только дозапрашивает задачу нашим токеном.
    """
    if not member_id:
        return None
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, b24_app_token FROM tenants "
            "WHERE b24_member_id = $1 AND status = 'active'", member_id)
    if row is None:
        return None
    if row["b24_app_token"]:
        expected = box.decrypt(
            row["b24_app_token"],
            box.aad("tenants", "b24_app_token", row["id"], member_id))
        if not app_token or not hmac.compare_digest(expected, app_token):
            return None
    return int(row["id"])
