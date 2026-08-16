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

from b24bot.api import ui_kit as ui
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


NEW_FIELD = "__new__"


def _field_select(fields: list[b24_fields.FieldRef], current: str | None,
                  name: str = "b24_field", field_id: str = "q-field") -> str:
    opts = ['<option value="">— в тело задачи —</option>',
            f'<option value="{NEW_FIELD}">+ создать новое поле в Битрикс24</option>']
    for f in fields:
        sel = " selected" if f.name == current else ""
        mark = " (свой)" if f.name.startswith("UF_") else ""
        opts.append(f'<option value="{esc_attr(f.name)}"{sel}>'
                    f"{esc_html(f.title)}{mark} · {esc_html(f.name)}</option>")
    if current and all(f.name != current for f in fields):
        # Поле убрали с портала или оно вне allowlist — показываем честно,
        # а не подменяем молча на «в тело задачи».
        opts.append(f'<option value="{esc_attr(current)}" selected>'
                    f"{esc_html(current)} — нет на портале</option>")
    return (f'<select class="input" id="{esc_attr(field_id)}" '
            f'name="{esc_attr(name)}">' + "".join(opts) + "</select>")


def _values_hint(fields: list[b24_fields.FieldRef], current: str | None) -> str:
    ref = next((f for f in fields if f.name == current), None)
    if ref is None or not ref.values:
        return ""
    pairs = ", ".join(f"{esc_html(v)} = <code>{esc_html(k)}</code>"
                      for k, v in ref.values.items())
    return ui.hint_html(f"Допустимые значения поля «{esc_html(ref.title)}»: "
                        f"{pairs}. Слева от знака равенства — что увидит человек "
                        f"в чате.")


def _question_form(session: str, template_id: int, q: asyncpg.Record | None,
                   fields: list[b24_fields.FieldRef]) -> str:
    qid = int(q["id"]) if q is not None else 0
    text = q["text"] if q is not None else ""
    kind = (q["answer_kind"] if q is not None else "text") or "text"
    required = bool(q["required"]) if q is not None else False
    b24_field = q["b24_field"] if q is not None else None
    options = _options_text(q["options"]) if q is not None else ""

    heading = "Изменить вопрос" if qid else "Новый вопрос"
    body = (
        f'<div class="f-group">'
        f'<label class="f-l" for="q-text">Текст вопроса</label>'
        f'<input class="input" type="text" id="q-text" name="text" '
        f'value="{esc_attr(text)}" maxlength="300" '
        f'placeholder="Что именно не работает?" required></div>'

        f'<div class="grid2">'
        f'<div class="f-group">'
        f'<label class="f-l" for="q-kind">Тип ответа</label>'
        f'<select class="input" id="q-kind" name="answer_kind">'
        f'<option value="text"{" selected" if kind != "choice" else ""}>'
        f"текстом</option>"
        f'<option value="choice"{" selected" if kind == "choice" else ""}>'
        f"выпадающий список</option></select></div>"
        f'<div class="f-group">'
        f'<label class="f-l" for="q-req">Обязательный</label>'
        f'<select class="input" id="q-req" name="required">'
        f'<option value="0"{"" if required else " selected"}>нет</option>'
        f'<option value="1"{" selected" if required else ""}>да</option>'
        f"</select></div></div>"

        f'<div class="f-group">'
        f'<label class="f-l" for="q-field">Куда уйдёт ответ</label>'
        f"{_field_select(fields, b24_field)}"
        f"{_values_hint(fields, b24_field)}</div>"

        f'<div class="f-group">'
        f'<label class="f-l" for="q-newf">Название нового поля</label>'
        f'<input class="input" type="text" id="q-newf" name="new_field_title" '
        f'maxlength="60" placeholder="только для «создать новое поле»" '
        f'aria-describedby="q-newf-h">'
        f'<p class="hint" id="q-newf-h">Заводит поле на самом портале: для '
        f"выпадающего списка — список с этими же вариантами, для текста — строку. "
        f"<b>Поле появится у всех задач портала</b>, а не только в этом проекте: "
        f"у задач Битрикс24 пользовательские поля общие.</p></div>"

        f'<div class="f-group">'
        f'<label class="f-l" for="q-opts">Варианты ответа, по одному в строке</label>'
        f'<textarea class="input mono" id="q-opts" name="options" rows="4" '
        f'placeholder="Высокий = 2&#10;Средний = 1&#10;Низкий = 0" '
        f'aria-describedby="q-opts-h">{esc_html(options)}</textarea>'
        f'<p class="hint" id="q-opts-h">Нужны только для выпадающего списка. '
        f"В чате каждый вариант станет кнопкой под вопросом.</p></div>"

        f'<div class="btn-row">'
        f'<button class="btn" type="submit">'
        f'{ui.icon("check" if qid else "plus", 15)}'
        f'{"Сохранить" if qid else "Добавить вопрос"}</button></div>')

    return (
        f'<form method="post" action="/b24/app/survey">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        f'<input type="hidden" name="action" value="save_question">'
        f'<input type="hidden" name="template_id" value="{template_id}">'
        f'<input type="hidden" name="question_id" value="{qid}">'
        f"{ui.panel(heading, body, icon_name='settings')}</form>")


def _tool(session: str, action: str, template_id: int, question_id: int,
          label: str, extra: str = "", *, variant: str = "ghost",
          icon_name: str = "", confirm: str = "", title: str = "") -> str:
    attrs = f' data-confirm="{esc_attr(confirm)}"' if confirm else ""
    attrs += f' title="{esc_attr(title)}"' if title else ""
    attrs += f' aria-label="{esc_attr(title)}"' if title else ""
    ico = ui.icon(icon_name, 15) if icon_name else ""
    return (
        '<form method="post" action="/b24/app/survey" class="inline">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        f'<input type="hidden" name="action" value="{esc_attr(action)}">'
        f'<input type="hidden" name="template_id" value="{template_id}">'
        f'<input type="hidden" name="question_id" value="{question_id}">'
        f'{extra}<button class="btn {variant}" type="submit"{attrs}>{ico}'
        f"{esc_html(label)}</button></form>")


async def render_editor(tenant_id: int, b24_user_id: int, template_id: int,
                        session: str, *, editing: int = 0,
                        message: str = "", kind: str = "ok") -> str:
    templates = await templates_of(tenant_id)
    current = next((t for t in templates if int(t["id"]) == template_id), None)
    if current is None and templates:
        current = templates[0]
    head = _head(session)
    if current is None:
        return (head + ui.panel(
            "Наборы вопросов",
            ui.empty("Ни одного набора нет",
                     "Создайте набор — из него бот соберёт опросник, который "
                     "увидит человек в чате.", icon_name="inbox"),
            icon_name="inbox") + _new_template_form(session))

    template_id = int(current["id"])
    items = await questions_of(tenant_id, template_id)
    fields, fields_error = await portal_fields(tenant_id, b24_user_id)

    msg = ui.banner(message, kind) if message else ""

    # Наборы — это переключатель, а не вкладки ARIA: каждая кнопка отправляет
    # форму и перезагружает страницу. Роль tab означала бы мгновенное
    # переключение панелей, которого здесь нет.
    chips = []
    for t in templates:
        own = " · свой" if t["tenant_id"] is not None else ""
        label = f"{t['title']}{own} · вопросов: {t['questions']}"
        if int(t["id"]) == template_id:
            chips.append(f'<button class="btn" type="button" aria-current="true" '
                         f"disabled>{esc_html(label)}</button>")
        else:
            chips.append(
                '<form method="post" action="/b24/app/survey" class="inline">'
                f'<input type="hidden" name="session" value="{esc_attr(session)}">'
                '<input type="hidden" name="action" value="open">'
                f'<input type="hidden" name="template_id" value="{t["id"]}">'
                f'<button class="btn sec" type="submit">{esc_html(label)}</button>'
                "</form>")

    rows = []
    for i, q in enumerate(items):
        kind_label = "список" if q["answer_kind"] == "choice" else "текстом"
        target = (f'→ <code>{esc_html(q["b24_field"])}</code>' if q["b24_field"]
                  else "→ в тело задачи")
        opts = _options_text(q["options"])
        opts_html = (f'<br><span class="muted">'
                     f'{esc_html(opts.replace(chr(10), " · "))}</span>'
                     if q["answer_kind"] == "choice" and opts else "")
        need = ui.badge("обязательный", "warn") if q["required"] else ""

        tools = "".join([
            _tool(session, "move", template_id, int(q["id"]), "",
                  '<input type="hidden" name="direction" value="up">',
                  icon_name="arrow-up", title="Поднять выше") if i else "",
            _tool(session, "move", template_id, int(q["id"]), "",
                  '<input type="hidden" name="direction" value="down">',
                  icon_name="arrow-down", title="Опустить ниже")
            if i < len(items) - 1 else "",
            _tool(session, "edit", template_id, int(q["id"]), "Изменить",
                  icon_name="settings"),
            _tool(session, "delete_question", template_id, int(q["id"]), "Удалить",
                  variant="danger", icon_name="x-circle",
                  confirm=f"Удалить вопрос «{q['text']}» из набора?"),
        ])

        rows.append(ui.item(
            f'{i + 1}. {esc_html(q["text"])}',
            sub_html=f"{esc_html(kind_label)} {target}{opts_html}",
            actions_html=f"{need}{tools}"))

    if rows:
        questions = f'<ul class="list">{"".join(rows)}</ul>'
    else:
        questions = ui.empty("В наборе пока нет вопросов",
                             "Добавьте первый вопрос формой ниже — бот задаст его "
                             "в чате при создании задачи.", icon_name="inbox")

    editing_row = next((q for q in items if int(q["id"]) == editing), None)
    form = (_question_form(session, template_id, editing_row, fields)
            if len(items) < MAX_QUESTIONS or editing_row is not None else
            ui.panel("Предел набора",
                     ui.hint(f"В наборе уже {MAX_QUESTIONS} вопросов — это предел. "
                             f"Столько никто не пройдёт до конца."),
                     icon_name="alert"))

    notes = ""
    if current["tenant_id"] is None:
        notes += ui.banner("<b>Это системный набор.</b> Первое изменение скопирует "
                           "его вам — общий останется нетронутым.", "info")
    if fields_error:
        notes += ui.banner(esc_html(fields_error), "warn")

    return (head + msg
            + ui.panel("Наборы вопросов",
                       f'<div class="btn-row">{"".join(chips)}</div>',
                       icon_name="inbox")
            + _new_template_form(session)
            + notes
            + ui.panel(str(current["title"]), questions, icon_name="folder",
                       flush=bool(rows))
            + form)


def _head(session: str) -> str:
    """Шапка с возвратом: без неё из конструктора нет пути назад."""
    back = (
        '<form method="post" action="/b24/app/back" class="inline">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        '<input type="hidden" name="tab" value="survey">'
        f'<button class="btn sec" type="submit">{ui.icon("arrow-left", 15)}'
        "К настройкам</button></form>")
    return (f'<header class="head"><div class="head-id">'
            f'<div class="mark">{ui.icon("inbox", 19)}</div>'
            f'<div class="head-t"><h1>Опросник</h1>'
            f'<div class="head-sub">вопросы, которые бот задаст в чате при '
            f"создании задачи</div></div></div>{back}</header>")


def _new_template_form(session: str) -> str:
    body = (
        f'<div class="f-group">'
        f'<label class="f-l" for="t-title">Название набора</label>'
        f'<input class="input" type="text" id="t-title" name="title" '
        f'maxlength="60" placeholder="Например: Заявка на доступ" required></div>'
        f'<div class="btn-row"><button class="btn sec" type="submit">'
        f'{ui.icon("plus", 15)}Создать набор</button></div>')
    return (
        '<form method="post" action="/b24/app/survey">'
        f'<input type="hidden" name="session" value="{esc_attr(session)}">'
        '<input type="hidden" name="action" value="add_template">'
        f"{ui.panel('Новый набор', body, icon_name='plus')}</form>")


# ----------------------------------------------------------------- действия
@router.post("/survey")
async def survey_action(session: str = Form(...), action: str = Form("open"),
                        template_id: int = Form(0), question_id: int = Form(0),
                        text: str = Form(""), answer_kind: str = Form("text"),
                        options: str = Form(""), required: str = Form("0"),
                        b24_field: str = Form(""), title: str = Form(""),
                        new_field_title: str = Form(""),
                        direction: str = Form("up")) -> HTMLResponse:
    from b24bot.api import app_ui

    sess = await app_ui.load_session(session)
    if sess is None:
        return app_ui.expired_page()

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
            new_field_title=new_field_title, title=title, direction=direction)
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
                 new_field_title: str = "",
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
            b24_field=b24_field, new_field_title=new_field_title)
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
                         options: str, required: bool, b24_field: str,
                         new_field_title: str = "") -> tuple[str, str]:
    clean = text.strip()
    if not clean:
        return "Текст вопроса пустой.", "err"

    kind = "choice" if answer_kind == "choice" else "text"
    parsed = parse_options(options) if kind == "choice" else []
    if kind == "choice" and not parsed:
        return ("Для выпадающего списка нужен хотя бы один вариант — "
                "иначе в чате не будет ни одной кнопки."), "err"

    field_type: str | None = None
    note, warning = "", ""

    if b24_field == NEW_FIELD:
        b24_field, field_type, parsed, note, failure = await _create_field(
            tenant_id, actor, new_field_title or clean, as_list=kind == "choice",
            options=parsed)
        if failure:
            return failure, "err"
    elif b24_field:
        fields, error = await portal_fields(tenant_id, actor)
        ref = next((f for f in fields if f.name == b24_field), None)
        if ref is None:
            return (error or f"Поле {esc_html(b24_field)} недоступно для привязки."), "err"
        field_type = ref.type
        if kind == "choice" and b24_field.startswith("UF_"):
            # Настройщик мог дописать вариант, которого в поле нет. Без сверки
            # ответ по нему ушёл бы в 0 — Битрикс принимает подпись молча.
            parsed, warning = await _sync_options(tenant_id, actor, b24_field, parsed)

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
    # Создание поля — не предупреждение, а результат: сообщение остаётся зелёным.
    # Жёлтым помечается только сверка вариантов, которая нашла расхождение.
    return f"Вопрос {what}.{where}{note}{warning}", ("warn" if warning else "ok")


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


async def _create_field(tenant_id: int, actor: int, label: str, *, as_list: bool,
                        options: list[dict[str, str]]
                        ) -> tuple[str, str | None, list[dict[str, str]], str, str]:
    """Завести поле на портале и привязать к нему вопрос.

    Возвращает `(имя, тип, варианты, примечание, отказ)`. Непустой отказ означает,
    что сохранять вопрос нельзя: привязать его не к чему.
    """
    try:
        client = await access.client_for_user(tenant_id, actor)
        async with client:
            name, field_type, mapped = await b24_fields.create_user_field(
                client, label, as_list=as_list, options=options)
    except b24_fields.FieldCreateError as exc:
        return "", None, options, "", str(exc)
    except errors.B24AccessDenied:
        return "", None, options, "", ("Битрикс24 не разрешил создавать поля задач "
                                       "под вашей учётной записью.")
    except Exception as exc:
        log.warning("создание поля не удалось: %s", str(exc)[:200])
        return "", None, options, "", "Не удалось создать поле в Битрикс24."

    await audit.record(tenant_id, "b24.userfield.create", actor_id=actor,
                       target=f"task_field:{name}",
                       detail={"название": label, "тип": field_type,
                               "вариантов": len(mapped)})
    note = (f" Создано поле «{esc_html(label)}» ({esc_html(name)}) — "
            f"оно появилось у всех задач портала.")
    return name, field_type, mapped or options, note, ""


async def _sync_options(tenant_id: int, actor: int, field_name: str,
                        options: list[dict[str, str]]
                        ) -> tuple[list[dict[str, str]], str]:
    try:
        client = await access.client_for_user(tenant_id, actor)
        async with client:
            mapped, warning = await b24_fields.sync_enum_options(
                client, field_name, options)
    except Exception as exc:
        log.warning("сверка вариантов поля %s не удалась: %s",
                    field_name, str(exc)[:150])
        return options, " Варианты списка не сверены с полем на портале."
    return mapped, (f" {warning}" if warning else "")
