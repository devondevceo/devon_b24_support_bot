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
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from b24bot.api import app_ui
from b24bot.api import ui_kit as ui
from b24bot.b24 import oauth
from b24bot.core.config import is_trusted_portal_domain
from b24bot.core.text import esc_html
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.domain import events as event_queue
from b24bot.domain import lifecycle, linking

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
    """REST-вызов. client_endpoint собираем САМИ из проверенного домена (И-4).

    Тело переехало в `b24/oauth.py`: тем же вызовом опознаётся человек, вошедший
    в портал из Telegram, а две копии проверки домена — это одна копия, которую
    однажды забудут поправить.
    """
    return await oauth.rest_call(domain, method, access_token, params)


# ------------------------------------------------------------------ сохранение
async def upsert_tenant(n: dict[str, str | None], granted_scope: str | None) -> int:
    domain = n["domain"] or ""
    if not is_trusted_portal_domain(domain):
        raise ValueError(f"недоверенный домен портала: {domain!r}")

    async with pool().acquire() as conn:
        # Переустановка возвращает теннанта в строй: пометка деинсталляции
        # снимается, пока данные не стёрты (lifecycle.PURGE_AFTER). После стирания
        # строки нет вовсе, и установка честно начинает с нуля.
        row = await conn.fetchrow(
            """
            INSERT INTO tenants (slug, name, b24_member_id, b24_domain, granted_scope,
                                 install_state)
            VALUES ($1, $2, $3, $4, $5, 'installed')
            ON CONFLICT (b24_member_id) DO UPDATE
              SET b24_domain = EXCLUDED.b24_domain,
                  granted_scope = COALESCE(EXCLUDED.granted_scope, tenants.granted_scope),
                  status = 'active',
                  uninstalled_at = NULL,
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


# Спайковый журнал payload (`log_payload` / SPIKE_LOG_PAYLOADS) удалён вместе с
# таблицей (миграция 0019): формат входа зафиксирован в docs/00-portal-facts.md
# §10.1, журнал вызовов ведёт `b24_call_log` — без тел и с ретенцией.


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


# ------------------------------------------------------- /b24/oauth/callback
@router.get("/oauth/callback")
async def oauth_callback(request: Request) -> Response:
    """Возврат человека из портала после входа: направление «Telegram → портал».

    Отдельный путь существует на случай, если локальное приложение всё-таки
    принимает свой `redirect_uri` (`B24_OAUTH_REDIRECT=true`). Пока это не
    проверено живым входом, портал вернёт `code` на зарегистрированный путь
    обработчика — поэтому его принимают ещё и `/b24/placement`, и `/b24/install`.
    Лишний открытый путь тут ничего не стоит: без нашего `state` он бесполезен.
    """
    return await _finish_link(dict(request.query_params))


def _is_oauth_return(payload: dict[str, str]) -> bool:
    """Портал вернул человека с кодом, а не открыл приложение.

    Признак — наш `state` вместе с `code` или `error`. Голый `code` без `state`
    сюда не относится: чужой он или наш, связать его не с чем.
    """
    return bool(payload.get("state")) and bool(payload.get("code") or payload.get("error"))


async def _finish_link(payload: dict[str, str]) -> Response:
    """Обмен кода и запись привязки. Один обработчик на все пути возврата."""
    if payload.get("error"):
        # Человек нажал «Отмена» на экране согласия — это не поломка.
        log.info("привязка не завершена, портал ответил %s", payload["error"][:64])
        return _link_notice("cancelled")

    result = await linking.complete(
        payload.get("state", ""), payload.get("code", ""),
        domain_hint=payload.get("domain"), member_hint=payload.get("member_id"))

    if isinstance(result, linking.Refusal):
        return _link_notice(result.code)

    await linking.notify_linked(result)
    return await _link_done_page(result)


LINK_REFUSALS: dict[str, tuple[str, str]] = {
    "state": ("Ссылка не подошла",
              "Она одноразовая и живёт 15 минут. Отправьте боту /link ещё раз — "
              "и пройдите по свежей ссылке."),
    "portal": ("Портал не подтвердил вход",
               "Битрикс24 не выдал доступ по этой ссылке. Попробуйте ещё раз; "
               "если повторится — покажите это администратору портала."),
    "mismatch": ("Это другой Битрикс24",
                 "Вы вошли не в тот портал, к которому подключён этот бот. "
                 "Войдите под учётной записью нужного портала."),
    "cancelled": ("Доступ не выдан",
                  "Вы отказались выдать доступ на экране Битрикс24. Ничего не "
                  "изменилось — отправьте боту /link, когда будете готовы."),
}


def _link_notice(code: str) -> HTMLResponse:
    title, text = LINK_REFUSALS.get(code, LINK_REFUSALS["state"])
    return app_ui.page(
        ui.panel("", ui.empty(title, text, icon_name="alert")), None,
        title="Привязка Telegram", embedded=False)


async def _link_done_page(result: linking.Linked) -> HTMLResponse:
    """Экран «готово». Отсюда человек возвращается в Telegram, а не в портал."""
    async with pool().acquire() as conn:
        username = await conn.fetchval(
            "SELECT username FROM tg_bots WHERE tenant_id = $1", result.tenant_id)

    back = (ui.link_button(f"https://t.me/{username}", "Вернуться в Telegram",
                           icon_name="send") if username else "")
    body = (
        ui.field("Портал", f"<code>{esc_html(result.portal_domain)}</code>")
        + ui.field("Пользователь Битрикс24",
                   f"{esc_html(result.display_name)} "
                   f"<code>{esc_html(result.b24_user_id)}</code>")
        + ui.hint("Задачи вы создаёте и меняете от своего имени: права режет сам "
                  "Битрикс24, мы их не расширяем.")
        + (f'<div class="btn-row">{back}</div>' if back else ""))
    return app_ui.page(
        ui.panel("Аккаунты связаны", body, icon_name="check-circle"), None,
        title="Привязка Telegram", embedded=False)


# ------------------------------------------------------------------ /b24/install
@router.post("/install")
@router.get("/install")
async def install(request: Request) -> Response:
    form = _flatten(dict(await request.form())) if request.method == "POST" else {}
    payload = {**dict(request.query_params), **form}
    if request.method == "GET" and _is_oauth_return(payload):
        return await _finish_link(payload)
    n = normalize(payload)

    if not n["member_id"] or not n["domain"]:
        log.warning("install без member_id/domain: %s", shape_for_log(payload))
        return _notice("err", "Установка не завершена",
                       "Портал не передал идентификатор. Откройте установку из "
                       "интерфейса Битрикс24, а не по прямой ссылке.", None)

    tenant_id = await upsert_tenant(n, n["scope"])
    await lifecycle.note_app_status(tenant_id, n["status"])

    installer_id = None
    if n["access_token"] and n["refresh_token"]:
        me = await b24_call(n["domain"], "user.current", n["access_token"])
        installer_id = (me.get("result") or {}).get("ID")
        if installer_id:
            await store_user_token(tenant_id, n["member_id"], int(installer_id),
                                   n["access_token"], n["refresh_token"], "service_admin")
            # Подписки на события и снимок подписки Маркета — прямо при установке:
            # до сих пор события привязывались руками при спайке, и установка на
            # новый портал не давала ни одного уведомления. Отказ не валит мастер
            # (он обязан быть идемпотентным) — суточный проход досоздаст.
            try:
                await lifecycle.ensure_event_bindings(tenant_id)
                await lifecycle.refresh_license(tenant_id)
            except Exception as exc:
                log.warning("установка: подписки/лицензия не доехали (теннант %s): "
                            "%s — досоздаст суточный проход", tenant_id,
                            str(exc)[:200])

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
@router.get("/placement")
async def placement_return(request: Request) -> Response:
    """GET на путь обработчика — это возврат из OAuth, а не открытие приложения.

    Приложение портал открывает POST-ом. Сюда же он приводит браузер человека
    после экрана согласия: `redirect_uri` у локального приложения по умолчанию и
    есть зарегистрированный путь обработчика. Всё остальное на этом пути —
    заход по прямой ссылке, и говорить в ответ надо ровно это.
    """
    payload = dict(request.query_params)
    if _is_oauth_return(payload):
        return await _finish_link(payload)
    return _notice("warn", "Откройте приложение в Битрикс24",
                   "Эта страница — часть приложения «Поддержка в Telegram». "
                   "По прямой ссылке она ничего не показывает.", None)


@router.post("/placement")
async def placement(request: Request) -> Response:
    form = _flatten(dict(await request.form()))
    payload = {**dict(request.query_params), **form}
    n = normalize(payload)

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

    # Буква статуса приложения приезжает в каждом открытии — бесплатный свежий
    # сигнал о подписке между суточными проверками app.info.
    await lifecycle.note_app_status(int(t["id"]), n["status"])

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
    """Права администратора портала спрашиваем у самого Битрикса (`b24/oauth.py`).

    Своих ролей у нас пока нет, а решать, кому можно вводить токен бота, надо уже
    сейчас. Дверей в приложение теперь две — открытие в портале и вход из
    Telegram, — и обе обязаны получать этот ответ одним и тем же способом.
    """
    return await oauth.is_portal_admin(domain, access_token)


# ------------------------------------------------------------------- /b24/events
@router.post("/events")
async def events(request: Request) -> Response:
    form = _flatten(dict(await request.form()))
    payload = {**dict(request.query_params), **form}
    n = normalize(payload)

    # События жизненного цикла приложения — отдельная ветка: у них нет задачи,
    # и класть их в очередь задач значило бы уронить их в «событие без задачи».
    event_name = str(n["event"] or "").upper()
    if event_name.startswith("ONAPP"):
        return await _app_event(event_name, n)

    # Отвечаем быстро и обрабатываем асинхронно: Битрикс не должен ждать, пока мы
    # сходим в портал за деталями задачи.
    tenant_id = await _tenant_by_member(n["member_id"], n["app_token"])
    if tenant_id is None:
        log.warning("событие от неизвестного или неподтверждённого портала")
        return JSONResponse({"ok": True})

    task_id = _task_id_of(payload)
    actor = payload.get("auth[user_id]")
    dedup = (f"{event_name}:{task_id}:{payload.get('data[FIELDS_AFTER][MESSAGE_ID]') or ''}"
             f":{payload.get('ts') or ''}")

    accepted = await event_queue.ingest(
        tenant_id, event_name, task_id,
        int(actor) if actor and str(actor).isdigit() else None, dedup)
    log.info("событие %s задача=%s принято=%s", event_name, task_id, accepted)
    return JSONResponse({"ok": True})


async def _app_event(event_name: str, n: dict[str, str | None]) -> Response:
    """`ONAPPUNINSTALL` / `ONAPPUPDATE` / `ONAPPTEST`.

    И-8 здесь работает в обе стороны. У `ONAPPUNINSTALL` `APPLICATION_TOKEN`
    ещё прежний — сверяем как у обычных событий, а решение подтверждает
    `lifecycle` дозапросом своим токеном. У `ONAPPUPDATE` токен в теле УЖЕ
    новый — сверять его не с чем, поэтому ветка пропускается без сверки, а
    подлинность доказывает смена версии в `app.info` (иначе перехваченным
    событием можно было бы подсунуть чужой токен и заглушить настоящие события).

    Ответ всегда `{"ok": true}`: разный ответ на «портал не найден» и «токен не
    совпал» работал бы оракулом, а повторов от Битрикса мы не просим.
    """
    member_id = n["member_id"]
    if not member_id:
        return JSONResponse({"ok": True})

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, b24_app_token FROM tenants "
            "WHERE b24_member_id = $1 AND status <> 'deleted'", member_id)
    if row is None:
        log.warning("событие %s от неизвестного портала", event_name)
        return JSONResponse({"ok": True})
    tenant_id = int(row["id"])

    if event_name == "ONAPPTEST":
        log.info("ONAPPTEST от теннанта %s", tenant_id)
    elif event_name == "ONAPPUNINSTALL":
        token_ok = True
        if row["b24_app_token"]:
            expected = box.decrypt(
                row["b24_app_token"],
                box.aad("tenants", "b24_app_token", tenant_id, member_id))
            token_ok = bool(n["app_token"]) and hmac.compare_digest(
                expected, n["app_token"] or "")
        if token_ok:
            await lifecycle.on_uninstall_event(tenant_id)
        else:
            log.warning("ONAPPUNINSTALL с несовпавшим APPLICATION_TOKEN, "
                        "теннант %s — игнорирую", tenant_id)
    elif event_name == "ONAPPUPDATE":
        await lifecycle.on_update_event(tenant_id, n["app_token"], n["scope"])
    else:
        log.info("событие приложения %s без обработчика (теннант %s)",
                 event_name, tenant_id)
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
