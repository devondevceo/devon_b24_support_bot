"""Конструктор опросника внутри приложения Битрикс24.

Теннант собирает свои наборы вопросов и связывает ответы с полями задачи.
Ответ на связанный вопрос уходит в поле, ответ на несвязанный — в тело задачи.

Три решения, которые стоит понимать до чтения кода:

* **Системные наборы форкаются, а не правятся.** `tenant_id IS NULL` — это набор,
  общий на всю инсталляцию. Правка одного теннанта меняла бы опросник всем
  остальным. Первое же изменение копирует набор себе вместе с вопросами; чтение
  уже умеет предпочитать свой набор системному с тем же кодом.
* **Список полей берётся с портала.** UF-поля у каждого портала свои, зашить их
  нельзя. Сужается allowlist-ом: из 67 полей задачи осмысленно принять ответ
  человека могут единицы (`b24/fields.py`).
* **Навигация формами, а не ссылками.** Сессия страницы лежит в POST-теле;
  в GET-параметре она попадала бы в логи, историю браузера и Referer.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import asyncpg
from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

from b24bot.b24 import errors
from b24bot.b24 import fields as b24_fields
from b24bot.core.text import esc_attr, esc_html
from b24bot.db.pool import pool
from b24bot.domain import access, audit

log = logging.getLogger(__name__)
router = APIRouter(prefix="/b24/app", tags=["bitrix24-app"])

MAX_QUESTIONS = 30      # длиннее опросник в чате никто не пройдёт
MAX_OPTIONS = 20        # вариантов на вопрос; столько же принимает бот
CODE_RE = re.compile(r"[^a-z0-9_]+")


# ------------------------------------------------------------------ выборка
async def templates_of(tenant_id: int) -> list[asyncpg.Record]:
    """Наборы, видимые теннанту: свои плюс системные, которые он ещё не форкал."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (t.code)
                   t.id, t.tenant_id, t.code, t.title, t.is_active, t.sort,
                   (SELECT count(*) FROM survey_questions q
                     WHERE q.template_id = t.id) AS questions
              FROM survey_templates t
             WHERE t.tenant_id = $1 OR t.tenant_id IS NULL
             ORDER BY t.code, t.tenant_id NULLS LAST
            """, tenant_id)
    return list(rows)


async def questions_of(tenant_id: int, template_id: int) -> list[asyncpg.Record]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT q.id, q.sort, q.code, q.text, q.answer_kind, q.options,
                   q.required, q.b24_field, q.b24_field_type
              FROM survey_questions q
              JOIN survey_templates t ON t.id = q.template_id
             WHERE q.template_id = $1 AND (t.tenant_id = $2 OR t.tenant_id IS NULL)
             ORDER BY q.sort, q.id
            """, template_id, tenant_id)
    return list(rows)


async def portal_fields(tenant_id: int, b24_user_id: int
                        ) -> tuple[list[b24_fields.FieldRef], str]:
    try:
        client = await access.client_for_user(tenant_id, b24_user_id)
        async with client:
            return await b24_fields.available(client), ""
    except errors.B24Error as exc:
        log.warning("не удалось получить поля задачи: %s", exc)
        return [], "Битрикс24 не отдал список полей — привязку сейчас не настроить."
    except Exception as exc:
        log.warning("не удалось получить поля задачи: %s", str(exc)[:150])
        return [], "Битрикс24 не отдал список полей — привязку сейчас не настроить."


# --------------------------------------------------------------------- форк
async def fork_if_system(tenant_id: int, template_id: int) -> int:
    """Вернуть id набора, который можно править. Системный — скопировать себе.

    Копия получает тот же `code`, поэтому в боте она сразу перекрывает системный
    набор: чтение выбирает `DISTINCT ON (code) ... ORDER BY tenant_id NULLS LAST`.
    """
    async with pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT id, tenant_id, code, title, sort, is_active FROM survey_templates "
            "WHERE id = $1 FOR UPDATE", template_id)
        if row is None:
            raise LookupError("набор не найден")
        if row["tenant_id"] is not None:
            if int(row["tenant_id"]) != tenant_id:
                raise PermissionError("чужой набор")
            return int(row["id"])

        existing = await conn.fetchval(
            "SELECT id FROM survey_templates WHERE tenant_id = $1 AND code = $2",
            tenant_id, row["code"])
        if existing is not None:
            return int(existing)

        new_id = await conn.fetchval(
            "INSERT INTO survey_templates (tenant_id, code, title, sort, is_active) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING id",
            tenant_id, row["code"], row["title"], row["sort"], row["is_active"])
        await conn.execute(
            """
            INSERT INTO survey_questions (tenant_id, template_id, sort, code, text,
                                          answer_kind, options, required,
                                          b24_field, b24_field_type)
            SELECT $1, $2, sort, code, text, answer_kind, options, required,
                   b24_field, b24_field_type
              FROM survey_questions WHERE template_id = $3
            """, tenant_id, new_id, template_id)
    log.info("набор %s форкнут теннанту %s как %s", template_id, tenant_id, new_id)
    return int(new_id)


# ------------------------------------------------------------------ рендер
def _options_text(raw: Any) -> str:
    """Варианты в textarea — по строке на вариант, `подпись = значение`."""
    items = json.loads(raw) if isinstance(raw, str) else (raw or [])
    lines = []
    for o in items:
        if not isinstance(o, dict):
            continue
        label, value = str(o.get("label") or ""), str(o.get("value") or "")
        lines.append(f"{label} = {value}" if value and value != label else label)
    return "\n".join(lines)


def parse_options(text: str) -> list[dict[str, str]]:
    """`Высокий = 2` -> {"label": "Высокий", "value": "2"}.

    Без знака равенства подпись и значение совпадают: для строкового поля это
    ровно то, что нужно, а для enum человек допишет значение сам, увидев подсказку
    со списком допустимых.
    """
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        label, _, value = line.partition("=")
        label, value = label.strip(), value.strip()
        if not label:
            continue
        out.append({"label": label[:60], "value": (value or label)[:200]})
        if len(out) >= MAX_OPTIONS:
            break
    return out


def _field_select(fields: list[b24_fields.FieldRef], current: str | None,
                  name: str = "b24_field") -> str:
    opts = ["<option value=''>— в тело задачи —</option>"]
    for f in fields:
        sel = " selected" if f.name == current else ""
        mark = " (свой)" if f.name.startswith("UF_") else ""
        opts.append(f"<option value='{esc_attr(f.name)}'{sel}>"
                    f"{esc_html(f.title)}{mark} · {esc_html(f.name)}</option>")
    if current and all(f.name != current for f in fields):
        # Поле убрали с портала или оно вне allowlist — показываем честно,
        # а не подменяем молча на «в тело задачи».
        opts.append(f"<option value='{esc_attr(current)}' selected>"
                    f"{esc_html(current)} — нет на портале</option>")
    return f"<select name='{name}'>" + "".join(opts) + "</select>"


def _values_hint(fields: list[b24_fields.FieldRef], current: str | None) -> str:
    ref = next((f for f in fields if f.name == current), None)
    if ref is None or not ref.values:
        return ""
    pairs = ", ".join(f"{esc_html(v)} = <code>{esc_html(k)}</code>"
                      for k, v in ref.values.items())
    return (f"<div class='hint'>Допустимые значения поля «{esc_html(ref.title)}»: "
            f"{pairs}. Слева от знака равенства — что увидит человек в чате.</div>")


def _question_form(session: str, template_id: int, q: asyncpg.Record | None,
                   fields: list[b24_fields.FieldRef]) -> str:
    qid = int(q["id"]) if q is not None else 0
    text = q["text"] if q is not None else ""
    kind = (q["answer_kind"] if q is not None else "text") or "text"
    required = bool(q["required"]) if q is not None else False
    b24_field = q["b24_field"] if q is not None else None
    options = _options_text(q["options"]) if q is not None else ""

    heading = "Изменить вопрос" if qid else "Новый вопрос"
    return (
        f"<form method='post' action='/b24/app/survey' class='qform'>"
        f"<input type='hidden' name='session' value='{esc_attr(session)}'>"
        f"<input type='hidden' name='action' value='save_question'>"
        f"<input type='hidden' name='template_id' value='{template_id}'>"
        f"<input type='hidden' name='question_id' value='{qid}'>"
        f"<div class='qhead'>{heading}</div>"
        f"<label>Текст вопроса"
        f"<input type='text' name='text' value='{esc_attr(text)}' maxlength='300' "
        f"placeholder='Что именно не работает?' required></label>"
        f"<div class='bind'>"
        f"<label>Тип ответа<select name='answer_kind'>"
        f"<option value='text'{' selected' if kind != 'choice' else ''}>"
        f"текстом</option>"
        f"<option value='choice'{' selected' if kind == 'choice' else ''}>"
        f"выпадающий список</option></select></label>"
        f"<label>Поле задачи{_field_select(fields, b24_field)}</label>"
        f"<label>Обязательный<select name='required'>"
        f"<option value='0'{'' if required else ' selected'}>нет</option>"
        f"<option value='1'{' selected' if required else ''}>да</option>"
        f"</select></label></div>"
        f"<label>Варианты ответа, по одному в строке"
        f"<textarea name='options' rows='4' "
        f"placeholder='Высокий = 2&#10;Средний = 1&#10;Низкий = 0'>"
        f"{esc_html(options)}</textarea></label>"
        f"{_values_hint(fields, b24_field)}"
        f"<div class='hint'>Варианты нужны только для выпадающего списка. "
        f"В чате каждый вариант станет кнопкой под вопросом.</div>"
        f"<button type='submit'>{'Сохранить' if qid else 'Добавить вопрос'}</button>"
        f"</form>")


def _tool(session: str, action: str, template_id: int, question_id: int,
          label: str, extra: str = "") -> str:
    return (
        "<form method='post' action='/b24/app/survey' style='display:inline'>"
        f"<input type='hidden' name='session' value='{esc_attr(session)}'>"
        f"<input type='hidden' name='action' value='{action}'>"
        f"<input type='hidden' name='template_id' value='{template_id}'>"
        f"<input type='hidden' name='question_id' value='{question_id}'>"
        f"{extra}<button class='link-btn' type='submit'>{label}</button></form>")


async def render_editor(tenant_id: int, b24_user_id: int, template_id: int,
                        session: str, *, editing: int = 0,
                        message: str = "", kind: str = "ok") -> str:
    templates = await templates_of(tenant_id)
    current = next((t for t in templates if int(t["id"]) == template_id), None)
    if current is None and templates:
        current = templates[0]
    if current is None:
        return ("<div class='card'><h1>Опросник</h1>"
                "<p>Ни одного набора вопросов нет.</p>"
                f"{_new_template_form(session)}</div>")

    template_id = int(current["id"])
    items = await questions_of(tenant_id, template_id)
    fields, fields_error = await portal_fields(tenant_id, b24_user_id)

    msg = f"<div class='msg {kind}'>{message}</div>" if message else ""

    tabs = []
    for t in templates:
        is_current = int(t["id"]) == template_id
        own = "" if t["tenant_id"] is None else " ✎"
        label = f"{esc_html(t['title'])}{own} · {t['questions']}"
        if is_current:
            tabs.append(f"<span class='tab on'>{label}</span>")
        else:
            tabs.append(
                "<form method='post' action='/b24/app/survey' style='display:inline'>"
                f"<input type='hidden' name='session' value='{esc_attr(session)}'>"
                f"<input type='hidden' name='action' value='open'>"
                f"<input type='hidden' name='template_id' value='{t['id']}'>"
                f"<button class='tab' type='submit'>{label}</button></form>")

    rows = []
    for i, q in enumerate(items):
        kind_label = ("список" if q["answer_kind"] == "choice" else "текстом")
        target = (f"→ <code>{esc_html(q['b24_field'])}</code>" if q["b24_field"]
                  else "<span class='muted'>→ в тело задачи</span>")
        opts = _options_text(q["options"])
        opts_html = (f"<div class='muted opts'>{esc_html(opts.replace(chr(10), ' · '))}"
                     f"</div>" if q["answer_kind"] == "choice" and opts else "")
        need = " · обязательный" if q["required"] else ""

        tools = "".join([
            _tool(session, "move", template_id, int(q["id"]), "↑",
                  "<input type='hidden' name='direction' value='up'>") if i else "",
            _tool(session, "move", template_id, int(q["id"]), "↓",
                  "<input type='hidden' name='direction' value='down'>")
            if i < len(items) - 1 else "",
            _tool(session, "edit", template_id, int(q["id"]), "изменить"),
            _tool(session, "delete_question", template_id, int(q["id"]), "удалить"),
        ])

        rows.append(
            f"<div class='q'><div class='qrow'><span>"
            f"<b>{i + 1}. {esc_html(q['text'])}</b><br>"
            f"<span class='muted'>{kind_label}{need}</span> {target}"
            f"{opts_html}</span><span class='tools'>{tools}</span></div></div>")

    if not rows:
        rows.append("<p class='muted'>В наборе пока нет вопросов.</p>")

    editing_row = next((q for q in items if int(q["id"]) == editing), None)
    form = (_question_form(session, template_id, editing_row, fields)
            if len(items) < MAX_QUESTIONS or editing_row is not None else
            f"<div class='hint'>В наборе уже {MAX_QUESTIONS} вопросов — "
            f"предел. Столько никто не пройдёт до конца.</div>")

    system_note = ("<div class='hint'>Это системный набор. Первое изменение "
                   "скопирует его вам — общий останется нетронутым.</div>"
                   if current["tenant_id"] is None else "")
    err = f"<div class='hint err'>{esc_html(fields_error)}</div>" if fields_error else ""

    return (
        f"{msg}"
        f"<div class='card'><h1>Опросник</h1>"
        f"<div class='tabs'>{''.join(tabs)}</div>"
        f"{_new_template_form(session)}</div>"
        f"<div class='card'><h2>{esc_html(current['title'])}</h2>"
        f"{system_note}{''.join(rows)}{err}</div>"
        f"<div class='card'>{form}</div>")


def _new_template_form(session: str) -> str:
    return (
        "<form method='post' action='/b24/app/survey' class='bind'>"
        f"<input type='hidden' name='session' value='{esc_attr(session)}'>"
        "<input type='hidden' name='action' value='add_template'>"
        "<label>Новый набор<input type='text' name='title' maxlength='60' "
        "placeholder='Например: Заявка на доступ' required></label>"
        "<button class='sec' type='submit'>Создать</button></form>")


# ----------------------------------------------------------------- действия
@router.post("/survey")
async def survey_action(session: str = Form(...), action: str = Form("open"),
                        template_id: int = Form(0), question_id: int = Form(0),
                        text: str = Form(""), answer_kind: str = Form("text"),
                        options: str = Form(""), required: str = Form("0"),
                        b24_field: str = Form(""), title: str = Form(""),
                        direction: str = Form("up")) -> HTMLResponse:
    from b24bot.api import app_ui

    sess = await app_ui.load_session(session)
    if sess is None:
        return app_ui.page("<div class='card'><h1>Сессия истекла</h1>"
                           "<p>Закройте и откройте приложение заново.</p></div>", None)

    async with pool().acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, b24_domain FROM tenants WHERE id = $1", sess["tenant_id"])
    tenant_id, actor = int(tenant["id"]), int(sess["b24_user_id"])
    domain = tenant["b24_domain"]

    # Опросник — это то, что увидит клиент в чате, и то, куда уедут поля задачи.
    # Настраивает его админ теннанта, а не любой сотрудник (docs/40-security.md §3).
    if not await app_ui.can_manage_admins(tenant_id, actor,
                                          bool(sess["is_portal_admin"])):
        body = await render_editor(tenant_id, actor, template_id, session,
                                   message="Настраивать опросник может "
                                           "администратор теннанта.", kind="err")
        return app_ui.page(body, domain)

    editing, message, kind = 0, "", "ok"
    try:
        template_id, editing, message, kind = await _apply(
            tenant_id, actor, action, template_id, question_id,
            text=text, answer_kind=answer_kind, options=options,
            required=required == "1", b24_field=b24_field.strip(),
            title=title, direction=direction)
    except PermissionError:
        message, kind = "Этот набор принадлежит другому теннанту.", "err"
    except LookupError:
        message, kind = "Набор или вопрос уже удалён.", "err"

    async with pool().acquire() as conn:
        fresh = await app_ui.issue_session(conn, tenant_id, actor,
                                           bool(sess["is_portal_admin"]))
    body = await render_editor(tenant_id, actor, template_id, fresh,
                               editing=editing, message=message, kind=kind)
    return app_ui.page(body, domain)


async def _apply(tenant_id: int, actor: int, action: str, template_id: int,
                 question_id: int, *, text: str, answer_kind: str, options: str,
                 required: bool, b24_field: str, title: str,
                 direction: str) -> tuple[int, int, str, str]:
    """Возвращает (набор, редактируемый вопрос, сообщение, вид сообщения)."""
    if action == "open":
        return template_id, 0, "", "ok"

    if action == "add_template":
        clean = title.strip()
        if not clean:
            return template_id, 0, "Название набора пустое.", "err"
        code = CODE_RE.sub("_", clean.lower())[:40] or "nabor"
        async with pool().acquire() as conn:
            new_id = await conn.fetchval(
                "INSERT INTO survey_templates (tenant_id, code, title, sort, is_active) "
                "VALUES ($1,$2,$3,(SELECT coalesce(max(sort),0)+10 FROM survey_templates "
                "WHERE tenant_id = $1),true) "
                "ON CONFLICT (tenant_id, code) WHERE tenant_id IS NOT NULL "
                "DO UPDATE SET title = EXCLUDED.title RETURNING id",
                tenant_id, code, clean[:60])
        await audit.record(tenant_id, "survey.template.add", actor_id=actor,
                           target=f"survey_template:{new_id}", detail={"название": clean})
        return int(new_id), 0, f"Набор «{esc_html(clean)}» создан.", "ok"

    if action == "edit":
        return template_id, question_id, "", "ok"

    editable = await fork_if_system(tenant_id, template_id)
    forked = editable != template_id

    if action == "save_question":
        message, kind = await _save_question(
            tenant_id, actor, editable, question_id, text=text,
            answer_kind=answer_kind, options=options, required=required,
            b24_field=b24_field)
        if forked:
            message += " Набор скопирован вам — системный не тронут."
        return editable, 0, message, kind

    if action == "delete_question":
        async with pool().acquire() as conn:
            gone = await conn.fetchval(
                "DELETE FROM survey_questions WHERE id = $1 AND template_id = $2 "
                "AND tenant_id = $3 RETURNING text",
                question_id if not forked else await _twin(conn, question_id, editable),
                editable, tenant_id)
        if gone is None:
            return editable, 0, "Вопрос уже удалён.", "ok"
        await audit.record(tenant_id, "survey.question.delete", actor_id=actor,
                           target=f"survey_template:{editable}", detail={"вопрос": gone})
        return editable, 0, "Вопрос удалён.", "ok"

    if action == "move":
        async with pool().acquire() as conn:
            qid = question_id if not forked else await _twin(conn, question_id, editable)
            await _move(conn, tenant_id, editable, int(qid or 0), direction)
        return editable, 0, "", "ok"

    return template_id, 0, "Неизвестное действие.", "err"


async def _twin(conn: asyncpg.Connection, question_id: int, new_template: int) -> int | None:
    """Тот же вопрос в свежем форке: id у копии другой, а `code` сохранился."""
    value = await conn.fetchval(
        "SELECT n.id FROM survey_questions n JOIN survey_questions o ON o.code = n.code "
        "WHERE o.id = $1 AND n.template_id = $2", question_id, new_template)
    return int(value) if value is not None else None


async def _save_question(tenant_id: int, actor: int, template_id: int,
                         question_id: int, *, text: str, answer_kind: str,
                         options: str, required: bool,
                         b24_field: str) -> tuple[str, str]:
    clean = text.strip()
    if not clean:
        return "Текст вопроса пустой.", "err"

    kind = "choice" if answer_kind == "choice" else "text"
    parsed = parse_options(options) if kind == "choice" else []
    if kind == "choice" and not parsed:
        return ("Для выпадающего списка нужен хотя бы один вариант — "
                "иначе в чате не будет ни одной кнопки."), "err"

    field_type = None
    if b24_field:
        fields, error = await portal_fields(tenant_id, actor)
        ref = next((f for f in fields if f.name == b24_field), None)
        if ref is None:
            return (error or f"Поле {esc_html(b24_field)} недоступно для привязки."), "err"
        field_type = ref.type

    async with pool().acquire() as conn, conn.transaction():
        if question_id:
            row = await conn.fetchrow(
                "UPDATE survey_questions SET text = $4, answer_kind = $5, "
                "options = $6, required = $7, b24_field = $8, b24_field_type = $9 "
                "WHERE id = $1 AND template_id = $2 AND tenant_id = $3 RETURNING code",
                question_id, template_id, tenant_id, clean[:300], kind,
                json.dumps(parsed, ensure_ascii=False), required,
                b24_field or None, field_type)
            if row is None:
                return "Вопрос не найден в этом наборе.", "err"
            what = "изменён"
        else:
            code = await _free_code(conn, template_id, clean)
            await conn.execute(
                "INSERT INTO survey_questions (tenant_id, template_id, sort, code, "
                "text, answer_kind, options, required, b24_field, b24_field_type) "
                "VALUES ($1,$2,(SELECT coalesce(max(sort),0)+10 FROM survey_questions "
                "WHERE template_id = $2),$3,$4,$5,$6,$7,$8,$9)",
                tenant_id, template_id, code, clean[:300], kind,
                json.dumps(parsed, ensure_ascii=False), required,
                b24_field or None, field_type)
            what = "добавлен"

    await audit.record(tenant_id, "survey.question.save", actor_id=actor,
                       target=f"survey_template:{template_id}",
                       detail={"вопрос": clean[:120], "поле": b24_field or "в тело",
                               "тип": kind})
    where = (f" Ответ уйдёт в поле {esc_html(b24_field)}." if b24_field
             else " Ответ уйдёт в тело задачи.")
    return f"Вопрос {what}.{where}", "ok"


async def _free_code(conn: asyncpg.Connection, template_id: int, text: str) -> str:
    """Код вопроса стабилен и переживает переименование: по нему хранится ответ."""
    base = CODE_RE.sub("_", text.lower()).strip("_")[:24] or "q"
    taken = {r["code"] for r in await conn.fetch(
        "SELECT code FROM survey_questions WHERE template_id = $1", template_id)}
    if base not in taken:
        return base
    for i in range(2, 100):
        if f"{base}_{i}" not in taken:
            return f"{base}_{i}"
    return f"{base}_{len(taken) + 1}"


async def _move(conn: asyncpg.Connection, tenant_id: int, template_id: int,
                question_id: int, direction: str) -> None:
    """Обмен позициями с соседом. Порядок вопросов — это порядок разговора."""
    rows = await conn.fetch(
        "SELECT id, sort FROM survey_questions WHERE template_id = $1 "
        "AND tenant_id = $2 ORDER BY sort, id", template_id, tenant_id)
    ids = [int(r["id"]) for r in rows]
    if question_id not in ids:
        return
    i = ids.index(question_id)
    j = i - 1 if direction == "up" else i + 1
    if not (0 <= j < len(ids)):
        return
    # Позиции могли совпасть (старые данные, копирование) — пересчитываем весь
    # набор, иначе обмен двух одинаковых значений ничего не меняет.
    ids[i], ids[j] = ids[j], ids[i]
    for position, qid in enumerate(ids):
        await conn.execute(
            "UPDATE survey_questions SET sort = $2 WHERE id = $1", qid, position * 10)
