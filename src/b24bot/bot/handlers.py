"""Сценарии бота: команды, создание задач, кнопки."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from b24bot.b24 import errors, mapping
from b24bot.b24.tokens import NeedsReauth
from b24bot.bot import (
    commands,
    comments,
    keyboards,
    survey,
    task_create,
    texts,
    views,
)
from b24bot.core.text import esc_html
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.domain import access, approvals, audit, miniapp, sync
from b24bot.domain import events as b24_events
from b24bot.domain import tasks as task_service
from b24bot.domain.context import (
    ChatContext,
    ProjectRef,
    authorize_task_for_chat,
    consume_token,
    issue_token,
    load_chat_context,
    load_chat_context_by_ref,
    remember_task,
)
from b24bot.tg import api as tg_api
from b24bot.tg import files as tg_files

log = logging.getLogger(__name__)

BIND_PAGE = 40    # сколько проектов портала помещается в одну клавиатуру


MAX_OPTIONS = 20  # вариантов ответа на один вопрос; больше не влезает в экран


def _help_text(*, private: bool) -> str:
    """Помощь строится из реестра команд: список в меню Telegram и в /help — один."""
    return texts.MSG_HELP_HEAD + "\n\n" + commands.render_help(private=private)


class Reply:
    """Ответ бота. Отправкой занимается вызывающий: так проще тестировать."""

    def __init__(self, text: str, *, buttons: list[list[dict[str, str]]] | None = None,
                 markup: dict[str, Any] | None = None,
                 edit: bool = False, remember_for_survey: int | None = None) -> None:
        self.text = text
        # id сессии опросника, которой надо запомнить message_id этого сообщения
        self.remember_for_survey = remember_for_survey
        self._markup = markup or ({"inline_keyboard": buttons} if buttons else None)
        # Списки и карточки живут в ОДНОМ сообщении, которое редактируется:
        # иначе чат превращается в ленту из десятков сообщений бота.
        self.edit = edit

    @property
    def markup(self) -> dict[str, Any] | None:
        return self._markup


# --------------------------------------------------------------------- разбор
def _text_of(msg: dict[str, Any]) -> str:
    return str(msg.get("text") or msg.get("caption") or "")


def _author(user: dict[str, Any]) -> str:
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(p for p in parts if p) or "без имени"
    username = user.get("username")
    return f"{name} (@{username})" if username else name


def _command(text: str, bot_username: str) -> tuple[str, str] | None:
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    cmd = head[1:].split("@")[0].lower()
    if "@" in head and not head.lower().endswith(f"@{bot_username.lower()}"):
        return None  # команда адресована другому боту в этой же группе
    return cmd, rest.strip()


def _mentions_bot(msg: dict[str, Any], bot_username: str) -> bool:
    text = _text_of(msg)
    for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
        if ent.get("type") == "mention":
            off, ln = int(ent["offset"]), int(ent["length"])
            if text[off:off + ln].lower() == f"@{bot_username.lower()}":
                return True
    return False


# ------------------------------------------------------------------- сценарии
async def on_message(bot: dict[str, Any], msg: dict[str, Any]) -> Reply | None:
    chat = msg.get("chat") or {}
    if chat.get("id") is None:
        return None
    chat_id = int(chat["id"])
    thread_id = msg.get("message_thread_id")
    user = msg.get("from") or {}
    tg_user_id = int(user.get("id") or 0)
    text = _text_of(msg)
    bot_username = str(bot["username"])

    ctx = await load_chat_context(chat_id, thread_id)
    cmd = _command(text, bot_username)

    if chat.get("type") == "private":
        return await _private(bot, cmd, tg_user_id, user, text)

    if ctx is None:
        return None

    if cmd:
        return await _group_command(bot, ctx, cmd, msg, tg_user_id, user)

    # Ответ на вопрос опросника. Проверяем ДО остальных правил, но принимаем
    # строго реплаем на своё же сообщение: иначе съедим обычную реплику коллеге.
    reply_to = msg.get("reply_to_message")
    if reply_to:
        answered = await _survey_answer(ctx, msg, reply_to, tg_user_id)
        if answered is not None:
            return answered

    # Реплай с упоминанием бота — основной триггер создания задачи.
    if reply_to and _mentions_bot(msg, bot_username):
        return await _create_from(ctx, reply_to, tg_user_id, msg.get("message_id"))
    return None


async def _resolve_task_arg(ctx: ChatContext, arg: str) -> int | None:
    """Номер задачи из аргумента команды или из ссылки на портал."""
    import re

    text = arg.strip()
    match = re.search(r"/task/view/(\d+)", text) or re.match(r"#?(\d+)", text)
    return int(match.group(1)) if match else None


async def _import_project(tenant_id: int, b24_user_id: int, b24_group_id: int,
                          fallback_name: str) -> int | None:
    """Завести проект в нашей базе. Клиент по умолчанию — по названию проекта.

    Иначе пришлось бы спрашивать клиента прямо в чате, а это лишний шаг в сценарии,
    который и так делают редко. Переназначить клиента можно в приложении.
    """
    async with pool().acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM projects WHERE tenant_id = $1 AND b24_group_id = $2",
            tenant_id, b24_group_id)
    if existing:
        return int(existing)

    name = fallback_name
    stages: list[sync.Stage] = []
    try:
        client = await access.client_for_user(tenant_id, b24_user_id)
        async with client:
            groups = await client.call("sonet_group.get",
                                       {"FILTER": {"ID": b24_group_id}})
            if groups:
                name = str(groups[0].get("NAME") or name)
            raw = await client.call("task.stages.get", {"entityId": b24_group_id})
            stages = sync.parse_stages(raw, b24_group_id)
    except (NeedsReauth, errors.B24Error) as exc:
        log.warning("импорт проекта %s: %s", b24_group_id, exc)
        if not name:
            return None

    async with pool().acquire() as conn, conn.transaction():
        client_row = await conn.fetchrow(
            "INSERT INTO clients (tenant_id, name) VALUES ($1,$2) "
            "ON CONFLICT (tenant_id, name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING id", tenant_id, name)
        pid = await conn.fetchval(
            "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, "
            "name_synced_at) VALUES ($1,$2,$3,$4,now()) "
            "ON CONFLICT (tenant_id, b24_group_id) DO UPDATE "
            "SET name = EXCLUDED.name, status = 'active' RETURNING id",
            tenant_id, client_row["id"], b24_group_id, name)
        await sync.apply_stages(conn, tenant_id, int(pid), stages)
    return int(pid)


async def _authorize_live(ctx: ChatContext, task_id: int, b24_user_id: int,
                          tg_user_id: int) -> ProjectRef | None:
    """Проверка доступа с дозапросом группы задачи.

    Кэш неполон по устройству, поэтому его отсутствие не может означать отказ.
    """
    if ctx.tenant_id is None:
        return None
    project = await authorize_task_for_chat(ctx.tenant_id, ctx.chat_ref, task_id)
    if project is not None:
        return project
    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            res = await client.call("tasks.task.get",
                                    {"taskId": task_id, "select": ["ID", "GROUP_ID"]})
    except (NeedsReauth, errors.B24Error):
        return None
    task = res.get("task", res) if isinstance(res, dict) else {}
    return await authorize_task_for_chat(
        ctx.tenant_id, ctx.chat_ref, task_id,
        group_id_hint=mapping.as_int(task.get("groupId")))


async def _comment_command(ctx: ChatContext, msg: dict[str, Any], arg: str,
                           tg_user_id: int) -> Reply:
    """/comment <номер> текст — комментарий в задачу."""
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    task_id = await _resolve_task_arg(ctx, arg)
    rest = arg.split(" ", 1)[1].strip() if " " in arg else ""
    if task_id is None or not rest:
        return Reply(texts.MSG_COMMENT_USAGE)

    project = await _authorize_live(ctx, task_id, b24_user_id, tg_user_id)
    if project is None:
        return Reply(texts.MSG_TASK_NOT_FOUND)

    author = _author(msg.get("from") or {})
    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            await comments.add(client, task_id, rest, author=author,
                               chat_title=ctx.title)
            note = ""
            attachments = tg_files.extract(msg)
            if attachments:
                count, rejected = await comments.transfer_files(
                    client, await _bot_token(ctx), ctx.tenant_id, task_id,
                    project.b24_group_id, b24_user_id, attachments,
                    f"comment-{ctx.chat_ref}-{msg.get('message_id')}")
                if count:
                    note = f"\nФайлов приложено: {count}"
                for reason in rejected:
                    note += f"\n{esc_html(reason)}"
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24AccessDenied as exc:
        return Reply(texts.MSG_NO_RIGHTS_B24.format(reason=esc_html(exc.description)))
    except errors.B24Error as exc:
        log.warning("комментарий не добавлен: %s", exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    return Reply(texts.MSG_COMMENT_ADDED.format(task_id=task_id) + note)


async def _bot_token(ctx: ChatContext) -> str:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT b.token, b.bot_id, b.tenant_id FROM tg_bots b "
            "JOIN tg_chats c ON c.bot_ref = b.id WHERE c.id = $1", ctx.chat_ref)
    if row is None:
        raise errors.B24Error("NO_BOT", "бот не найден")
    return box.decrypt(row["token"],
                       box.aad("tg_bots", "token", row["tenant_id"], row["bot_id"]))


async def _discussion(ctx: ChatContext, arg: str, tg_user_id: int) -> Reply:
    """Показать обсуждение задачи. Комментарии лежат в чате задачи, не в форуме."""
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    task_id = await _resolve_task_arg(ctx, arg)
    if task_id is None:
        return Reply(texts.MSG_COMMENT_USAGE)
    if await _authorize_live(ctx, task_id, b24_user_id, tg_user_id) is None:
        return Reply(texts.MSG_TASK_NOT_FOUND)

    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            items = await comments.read_discussion(client, task_id)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24Error:
        return Reply(texts.MSG_B24_UNAVAILABLE)
    return Reply(comments.render_discussion(task_id, items))


async def _survey_start(ctx: ChatContext, tg_user_id: int) -> Reply:
    """Выбор категории обращения."""
    if not ctx.is_active or not ctx.has_binding:
        return Reply(texts.MSG_NO_PROJECT)
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    if await access.linked_b24_user(ctx.tenant_id, tg_user_id) is None:
        return Reply(texts.MSG_NOT_LINKED)

    rows = []
    for template_id, title in await survey.categories(ctx.tenant_id):
        token = await issue_token("survey", tenant_id=ctx.tenant_id,
                                  owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                                  payload={"template_id": template_id},
                                  ttl=timedelta(minutes=30))
        rows.append([{"text": title, "callback_data": f"s:{token}"}])
    return Reply(texts.MSG_SURVEY_CHOOSE, markup={"inline_keyboard": rows})


async def _survey_begin(ctx: ChatContext, tg_user_id: int, template_id: int) -> Reply:
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    project_id = ctx.projects[0].id if len(ctx.projects) == 1 else None
    session = await survey.start(ctx.tenant_id, ctx.chat_ref, ctx.thread_id,
                                 tg_user_id, template_id, project_id)
    items = await survey.questions(ctx.tenant_id, template_id)
    if not items:
        await survey.finish(session.id, "cancelled")
        return Reply(texts.MSG_SURVEY_EMPTY)
    return Reply(survey.question_text(items[0], 0, len(items)),
                 markup=await _survey_kb(ctx, tg_user_id, session.id, items[0]),
                 remember_for_survey=session.id)


async def _survey_kb(ctx: ChatContext, tg_user_id: int, session_id: int,
                     q: survey.Question) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []

    # Выпадающий список в чате — это кнопки под вопросом. По одной в ряд:
    # варианты бывают длинными, а Telegram режет подпись без предупреждения.
    for i, option in enumerate(q.options[:MAX_OPTIONS] if q.kind == "choice" else []):
        token = await issue_token("survey_pick", tenant_id=ctx.tenant_id,
                                  owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                                  payload={"session_id": session_id, "index": i},
                                  ttl=timedelta(minutes=30))
        label = str(option.get("label") or option.get("value") or "—")
        rows.append([{"text": label[:60], "callback_data": f"p:{token}"}])

    row = []
    if not q.required:
        token = await issue_token("survey_skip", tenant_id=ctx.tenant_id,
                                  owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                                  payload={"session_id": session_id},
                                  ttl=timedelta(minutes=30))
        row.append({"text": "⏭ Пропустить", "callback_data": f"k:{token}"})
    cancel = await issue_token("survey_cancel", tenant_id=ctx.tenant_id,
                               owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                               payload={"session_id": session_id},
                               ttl=timedelta(minutes=30))
    row.append({"text": "❌ Отменить", "callback_data": f"x:{cancel}"})
    rows.append(row)
    return {"inline_keyboard": rows}


async def _survey_answer(ctx: ChatContext, msg: dict[str, Any],
                         reply_to: dict[str, Any], tg_user_id: int) -> Reply | None:
    """Ответ на вопрос. None означает «это не про опросник, обрабатывай дальше»."""
    if ctx.tenant_id is None:
        return None
    session = await survey.active_for(ctx.chat_ref, msg.get("message_thread_id"),
                                      tg_user_id)
    if session is None:
        return None
    if session.last_message_id != reply_to.get("message_id"):
        # Реплай на что-то другое: человек просто разговаривает с коллегами.
        return None

    text = _text_of(msg).strip()
    if not text:
        return None

    items = await survey.questions(ctx.tenant_id, session.template_id)
    if session.step >= len(items):
        return None

    q = items[session.step]
    if q.kind == "choice" and q.options:
        # Человек напечатал вместо нажатия. Принимаем, если это в точности одна
        # из подписей: иначе в поле Битрикса уедет текст вместо значения.
        match = next((o for o in q.options
                      if str(o.get("label", "")).strip().lower() == text.lower()), None)
        if match is None:
            return Reply(texts.MSG_SURVEY_PICK_BUTTON,
                         markup=await _survey_kb(ctx, tg_user_id, session.id, q),
                         remember_for_survey=session.id)
        text = str(match.get("value") or match.get("label") or "")

    await survey.record(session, q.code, text)
    return await _survey_next(ctx, session, items, tg_user_id)


async def _survey_next(ctx: ChatContext, session: survey.Session,
                       items: list[survey.Question], tg_user_id: int) -> Reply:
    if session.step < len(items):
        q = items[session.step]
        return Reply(survey.question_text(q, session.step, len(items)),
                     markup=await _survey_kb(ctx, tg_user_id, session.id, q),
                     remember_for_survey=session.id)

    # Вопросы кончились — показываем черновик и ждём подтверждения.
    # Описание собирается заново в момент создания: между превью и нажатием
    # человек мог ответить ещё раз.
    built = survey.assemble(items, session.answers)
    title = built.title
    project = next((p for p in ctx.projects if p.id == session.project_id),
                   ctx.projects[0] if ctx.projects else None)
    if project is None:
        await survey.finish(session.id, "cancelled")
        return Reply(texts.MSG_NO_PROJECT)

    create = await issue_token("survey_create", tenant_id=ctx.tenant_id,
                               owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                               payload={"session_id": session.id,
                                        "project_id": project.id},
                               ttl=timedelta(hours=24))
    cancel = await issue_token("survey_cancel", tenant_id=ctx.tenant_id,
                               owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                               payload={"session_id": session.id},
                               ttl=timedelta(hours=24))
    return Reply(survey.render_preview(title, items, session.answers, project.name),
                 markup=keyboards.confirm(create, cancel))


async def _survey_create(ctx: ChatContext, tg_user_id: int, session_id: int,
                         project_id: int) -> Reply:
    """Создание задачи из собранных ответов."""
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT template_id, answers FROM survey_sessions WHERE id = $1 "
            "AND state = 'active'", session_id)
    if row is None:
        return Reply(texts.MSG_DIALOG_EXPIRED)

    answers = row["answers"]
    answers = json.loads(answers) if isinstance(answers, str) else dict(answers or {})
    items = await survey.questions(ctx.tenant_id, int(row["template_id"]))
    built = survey.assemble(items, answers)

    project = next((p for p in ctx.projects if p.id == project_id), None)
    if project is None:
        return Reply(texts.MSG_NO_PROJECT)

    draft = task_create.Draft(
        title=built.title, description=built.description,
        idem_key=f"survey-{session_id}", source_message_id=None,
        fields=built.fields)

    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            task, created = await task_create.create(
                client, ctx.tenant_id, project, draft, b24_user_id)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24AccessDenied as exc:
        return Reply(texts.MSG_NO_RIGHTS_B24.format(reason=esc_html(exc.description)))
    except errors.B24Error as exc:
        log.warning("создание задачи из опросника не удалось: %s", exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    await survey.finish(session_id, "done")
    if not created:
        return Reply(texts.MSG_TASK_EXISTS.format(
            task_id=task.get("id"), title=esc_html(task.get("title") or "")))
    return Reply(texts.MSG_TASK_CREATED.format(
        task_id=task.get("id"), title=esc_html(task.get("title") or ""),
        project=esc_html(project.name),
        responsible=esc_html((task.get("responsible") or {}).get("name") or b24_user_id)))


async def _menu_tokens(ctx: ChatContext, tg_user_id: int) -> dict[str, str]:
    """Токены под кнопки меню. Общие для чата: меню закрепляют, им пользуются все."""
    actions = ("status", "overdue", "mine", "all", "new")
    out = {}
    for action in actions:
        out[action] = await issue_token(
            "menu", tenant_id=ctx.tenant_id, chat_ref=ctx.chat_ref,
            payload={"action": action}, single_use=False,
            ttl=timedelta(days=365))
    return out


async def _help_reply(ctx: ChatContext, tg_user_id: int) -> Reply:
    if not ctx.is_active or not ctx.has_binding:
        return Reply(_help_text(private=False) if ctx.is_active
                     else texts.MSG_START_GROUP)
    tokens = await _menu_tokens(ctx, tg_user_id)
    projects = ", ".join(esc_html(p.name) for p in ctx.projects)
    app_url = await miniapp.link_for_chat(ctx.tenant_id or 0, ctx.chat_ref,
                                          thread_id=ctx.thread_id)
    text = (f"{_help_text(private=False)}\n\n"
            f"<b>Проекты этого чата:</b> {projects}\n"
            f"<i>Закрепите это сообщение — кнопки будут всегда под рукой.</i>")
    if app_url:
        text += texts.MSG_APP_HINT
    return Reply(text, markup=keyboards.help_menu(tokens, app_url))


async def _open_summary(ctx: ChatContext, tg_user_id: int, action: str) -> Reply:
    """Сводка и списки. Всё через личный токен: видно то, что видно человеку."""
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    group_ids = [p.b24_group_id for p in ctx.projects]
    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            tasks = await views.fetch_open(client, group_ids)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24Error as exc:
        log.warning("не удалось получить задачи: %s", exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    app_url = await miniapp.link_for_chat(ctx.tenant_id, ctx.chat_ref,
                                          thread_id=ctx.thread_id)
    if action == "status":
        text = await views.render_summary(ctx.tenant_id, ctx.projects, tasks)
        tokens = await _menu_tokens(ctx, tg_user_id)
        return Reply(text, markup=keyboards.help_menu(tokens, app_url), edit=True)

    if action == "overdue":
        tasks = [t for t in tasks if views.is_overdue(t)]
        title = "🔥 Просроченные"
    elif action == "mine":
        tasks = [t for t in tasks
                 if str(t.get("responsibleId")) == str(b24_user_id)]
        title = "👤 Мои задачи"
    else:
        title = "📋 Все открытые задачи"

    numbers = []
    for i, t in enumerate(views.flatten_for_buttons(tasks), start=1):
        token = await issue_token("task", tenant_id=ctx.tenant_id, chat_ref=ctx.chat_ref,
                                  payload={"task_id": int(t["id"])}, single_use=False,
                                  ttl=timedelta(days=7))
        numbers.append((token, str(i)))

    back = await issue_token("menu", tenant_id=ctx.tenant_id, chat_ref=ctx.chat_ref,
                             payload={"action": "status"}, single_use=False,
                             ttl=timedelta(days=7))
    nav = [{"text": "◀️ Назад", "callback_data": f"m:{back}"}]
    if app_url:
        nav.append(keyboards.url_button("🧩 Приложение", app_url))
    return Reply(views.render_list(tasks, title=title),
                 markup=keyboards.task_list(numbers, nav), edit=True)


async def _open_card(ctx: ChatContext, tg_user_id: int, task_id: int) -> Reply:
    """Карточка задачи. Инвариант И-3: проверка принадлежности чату обязательна."""
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    # Читаем задачу ЕГО токеном: если прав нет, Битрикс откажет сам. Проверка
    # принадлежности чату идёт следом, по фактической группе задачи.
    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            res = await client.call("tasks.task.get", {
                "taskId": task_id, "select": mapping.TASK_SELECT_FULL})
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24AccessDenied:
        return Reply(texts.MSG_TASK_NOT_FOUND)
    except errors.B24Error:
        return Reply(texts.MSG_B24_UNAVAILABLE)

    task = res.get("task", res) if isinstance(res, dict) else {}
    project = await authorize_task_for_chat(
        ctx.tenant_id, ctx.chat_ref, task_id,
        group_id_hint=mapping.as_int(task.get("groupId")))
    if project is None:
        return Reply(texts.MSG_TASK_NOT_FOUND)
    return await _render_card(ctx, tg_user_id, b24_user_id, task, project)


async def _render_card(ctx: ChatContext, tg_user_id: int, b24_user_id: int,
                       task: dict[str, Any], project: ProjectRef,
                       note: str = "") -> Reply:
    """Отрисовка карточки по УЖЕ прочитанной задаче.

    Отдельно от чтения, потому что после изменения задача уже перечитана: лишний
    `tasks.task.get` стоит и частотного лимита, и `operating`.
    """
    assert ctx.tenant_id is not None
    task_id = mapping.as_int(task.get("id")) or 0
    await remember_task(ctx.tenant_id, project, task)

    allowed = mapping.allowed_actions(task)

    tokens = {}
    for act in ("complete", "start", "pause", "refresh"):
        tokens[act] = await issue_token(
            "action", tenant_id=ctx.tenant_id, owner_tg_id=tg_user_id,
            chat_ref=ctx.chat_ref, payload={"task_id": task_id, "act": act},
            single_use=(act != "refresh"), ttl=timedelta(hours=12))
    tokens["edit"] = await _edit_token(ctx, tg_user_id, task_id, "menu")
    tokens["back"] = await issue_token(
        "menu", tenant_id=ctx.tenant_id, chat_ref=ctx.chat_ref,
        payload={"action": "all"}, single_use=False, ttl=timedelta(days=7))

    domain = await _tenant_domain(ctx.tenant_id)
    app_url = await miniapp.link_for_chat(ctx.tenant_id, ctx.chat_ref,
                                          thread_id=ctx.thread_id, task_id=task_id)
    text = views.render_card(task, project)
    return Reply(f"{note}\n\n{text}" if note else text,
                 markup=keyboards.task_card(
                     tokens, allowed=allowed,
                     portal_url=views.portal_task_url(domain, task_id, b24_user_id),
                     app_url=app_url),
                 edit=True)


# Действия карточки: метод портала, ожидаемый статус после него и имя в журнале.
# Имена аудита общие с мини-аппом (`api/miniapp.py`): одно и то же действие обязано
# называться одинаково, откуда бы его ни сделали, иначе журнал бесполезен для разбора.
# Расхождение ловит тест-страж `tests/test_audit_names.py`.
ACTION_METHODS = {"complete": "tasks.task.complete", "start": "tasks.task.start",
                  "pause": "tasks.task.pause"}
ACTION_STATUS = {"complete": 5, "start": 3, "pause": 2}
ACTION_AUDIT = {"complete": "task.complete", "start": "task.status.change",
                "pause": "task.status.change"}


async def _task_action(ctx: ChatContext, tg_user_id: int, task_id: int,
                       act: str) -> Reply:
    """Смена состояния задачи спец-методами.

    Сырой update STATUS не проводит бизнес-логику и права, поэтому используем
    tasks.task.complete/start/pause — как и предписывает портал.
    """
    if act == "refresh":
        return await _open_card(ctx, tg_user_id, task_id)
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)
    method = ACTION_METHODS.get(act)
    if method is None:
        return Reply(texts.MSG_DIALOG_EXPIRED)

    # Своё же изменение не должно вернуться уведомлением в этот же чат.
    expected_status = ACTION_STATUS.get(act)
    if expected_status is not None:
        await b24_events.suppress_echo(ctx.tenant_id, task_id, "STATUS",
                                       expected_status, b24_user_id)

    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            check = await client.call("tasks.task.get",
                                      {"taskId": task_id, "select": ["ID", "GROUP_ID"]})
            checked = check.get("task", check) if isinstance(check, dict) else {}
            project = await authorize_task_for_chat(
                ctx.tenant_id, ctx.chat_ref, task_id,
                group_id_hint=mapping.as_int(checked.get("groupId")))
            if project is None:
                return Reply(texts.MSG_TASK_NOT_FOUND)
            await client.call(method, {"taskId": task_id})
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24AccessDenied as exc:
        return Reply(texts.MSG_NO_RIGHTS_B24.format(reason=esc_html(exc.description)))
    except errors.B24Error as exc:
        log.warning("действие %s над задачей %s не удалось: %s", act, task_id, exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    # Пишется после `except`: запись о мутации, которой не было, хуже её отсутствия.
    await audit.record(ctx.tenant_id, ACTION_AUDIT.get(act, "task.status.change"),
                       actor_id=b24_user_id, actor_tg_id=tg_user_id,
                       target=f"task:{task_id}", project_id=project.id,
                       detail={"act": act, "source": "bot"})

    return await _open_card(ctx, tg_user_id, task_id)


# ------------------------------------------------------------- редактирование
EDIT_AUDIT = {"responsible_id": "task.responsible.change",
              "deadline": "task.deadline.change",
              "priority": "task.priority.change"}
PRIORITY_LABELS = {0: "низкий", 1: "средний", 2: "высокий"}
DEADLINE_LABELS = {"today": "сегодня", "tomorrow": "завтра", "in3": "через 3 дня",
                   "week": "через неделю", "clear": "снят"}
ASSIGNEE_PAGE = 12  # больше кнопок в один экран телефона всё равно не влезает


async def _edit_token(ctx: ChatContext, tg_user_id: int, task_id: int, act: str,
                      value: Any = None) -> str:
    """Токен кнопки редактирования.

    Меню можно жать сколько угодно, а само изменение — одноразовое: два нажатия
    подряд по «завтра» безобидны, но одноразовость здесь стоит дёшево и снимает
    целый класс вопросов «почему сработало дважды».
    """
    payload: dict[str, Any] = {"task_id": task_id, "act": act}
    if value is not None:
        payload["value"] = value
    return await issue_token("edit", tenant_id=ctx.tenant_id, owner_tg_id=tg_user_id,
                             chat_ref=ctx.chat_ref, payload=payload,
                             single_use=act.startswith("set_"),
                             ttl=timedelta(hours=12))


async def _edit(ctx: ChatContext, tg_user_id: int, payload: dict[str, Any]) -> Reply:
    """Изменение срока, ответственного и приоритета кнопками.

    Свободного ввода в группе нет: бот не может ждать ответа от одного человека,
    не перехватывая чужие реплики (той же причиной живёт правило опросника —
    отвечать реплаем). Произвольная дата и остальные поля — в мини-аппе.
    """
    task_id = int(payload["task_id"])
    act = str(payload.get("act") or "menu")
    if act == "back":
        return await _open_card(ctx, tg_user_id, task_id)

    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    patch: dict[str, Any] = {}
    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            task = await task_service.read(client, task_id)
            project = await authorize_task_for_chat(
                ctx.tenant_id, ctx.chat_ref, task_id,
                group_id_hint=mapping.as_int(task.get("groupId")))
            if project is None:
                return Reply(texts.MSG_TASK_NOT_FOUND)

            if act in ("menu", "deadline_menu", "priority_menu"):
                return await _edit_menu(ctx, tg_user_id, task_id, act, task)
            if act == "assignee_menu":
                members = await task_service.group_members(client,
                                                           project.b24_group_id)
                return await _assignee_menu(ctx, tg_user_id, task_id, members)

            built = _edit_patch(act, payload.get("value"), task)
            if built is None:
                return Reply(texts.MSG_DIALOG_EXPIRED)
            patch = built
            fresh, missed = await task_service.apply_patch(
                client, ctx.tenant_id, task_id, patch,
                actor_b24_user_id=b24_user_id)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24AccessDenied as exc:
        return Reply(texts.MSG_NO_RIGHTS_B24.format(reason=esc_html(exc.description)))
    except task_service.Invalid as exc:
        return Reply(esc_html(exc.message))
    except errors.B24Error as exc:
        log.warning("правка задачи %s не удалась: %s", task_id, exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    # Словарь действий общий с мини-аппом: одно и то же действие обязано называться
    # одинаково, откуда бы его ни сделали, иначе журнал бесполезен для разбора.
    for field in patch:
        await audit.record(ctx.tenant_id, EDIT_AUDIT.get(field, "task.edit"),
                           actor_id=b24_user_id, actor_tg_id=tg_user_id,
                           target=f"task:{task_id}", project_id=project.id,
                           detail={"field": field, "not_applied": missed,
                                   "source": "bot"})

    note = texts.MSG_EDIT_DONE.format(what=esc_html(_edit_summary(act, payload, patch)))
    if missed:
        note += texts.MSG_EDIT_NOT_APPLIED.format(fields=esc_html(", ".join(missed)))
    return await _render_card(ctx, tg_user_id, b24_user_id, fresh, project, note)


def _edit_patch(act: str, value: Any, task: dict[str, Any]) -> dict[str, Any] | None:
    """Что именно менять. Срок считается в часовом поясе портала, а не сервера."""
    if act == "set_deadline":
        offset = task_service.offset_of(task.get("createdDate"),
                                        task.get("changedDate"),
                                        task.get("deadline"))
        return {"deadline": task_service.preset_deadline(str(value), offset)}
    if act == "set_priority":
        return task_service.validate_patch({"priority": value})
    if act == "set_responsible":
        return task_service.validate_patch({"responsible_id": value})
    return None


def _edit_summary(act: str, payload: dict[str, Any], patch: dict[str, Any]) -> str:
    if act == "set_deadline":
        return f"срок — {DEADLINE_LABELS.get(str(payload.get('value')), 'изменён')}"
    if act == "set_priority":
        return f"приоритет — {PRIORITY_LABELS.get(int(patch['priority']), '?')}"
    return "ответственный"


async def _edit_menu(ctx: ChatContext, tg_user_id: int, task_id: int, act: str,
                     task: dict[str, Any]) -> Reply:
    app_url = await miniapp.link_for_chat(ctx.tenant_id or 0, ctx.chat_ref,
                                          thread_id=ctx.thread_id, task_id=task_id)
    if act == "menu":
        tokens = {name: await _edit_token(ctx, tg_user_id, task_id, name)
                  for name in ("deadline_menu", "assignee_menu", "priority_menu",
                               "back")}
        return Reply(texts.MSG_EDIT_MENU.format(task_id=task_id),
                     markup=keyboards.edit_menu(tokens, app_url), edit=True)

    if act == "deadline_menu":
        tokens = {kind: await _edit_token(ctx, tg_user_id, task_id, "set_deadline",
                                          kind)
                  for kind in ("today", "tomorrow", "in3", "week", "clear")}
        tokens["back"] = await _edit_token(ctx, tg_user_id, task_id, "menu")
        current = views.fmt_date(task.get("deadline")) if task.get("deadline") else "нет"
        return Reply(texts.MSG_EDIT_DEADLINE.format(task_id=task_id)
                     + f"\nСейчас: {esc_html(current)}",
                     markup=keyboards.deadline_menu(tokens, app_url), edit=True)

    tokens = {f"p{value}": await _edit_token(ctx, tg_user_id, task_id, "set_priority",
                                             value)
              for value in (0, 1, 2)}
    tokens["back"] = await _edit_token(ctx, tg_user_id, task_id, "menu")
    now = mapping.PRIORITY_TITLES.get(mapping.as_int(task.get("priority")) or 0, "—")
    return Reply(texts.MSG_EDIT_PRIORITY.format(task_id=task_id)
                 + f"\nСейчас: {esc_html(now)}",
                 markup=keyboards.priority_menu(tokens), edit=True)


async def _assignee_menu(ctx: ChatContext, tg_user_id: int, task_id: int,
                         members: list[dict[str, Any]]) -> Reply:
    if not members:
        return Reply(texts.MSG_EDIT_NO_MEMBERS)

    shown = members[:ASSIGNEE_PAGE]
    people = []
    for m in shown:
        token = await _edit_token(ctx, tg_user_id, task_id, "set_responsible", m["id"])
        label = m["name"] + (f" · {m['position']}" if m["position"] else "")
        people.append((token, label))
    back = await _edit_token(ctx, tg_user_id, task_id, "menu")

    text = texts.MSG_EDIT_ASSIGNEE.format(task_id=task_id)
    if len(members) > len(shown):
        # Молчаливое усечение читается как баг продукта — говорим числом.
        text += texts.MSG_EDIT_TRUNCATED.format(shown=len(shown), total=len(members))
    app_url = await miniapp.link_for_chat(ctx.tenant_id or 0, ctx.chat_ref,
                                          thread_id=ctx.thread_id, task_id=task_id)
    return Reply(text, markup=keyboards.people_menu(people, back, app_url), edit=True)


async def _tenant_domain(tenant_id: int) -> str:
    async with pool().acquire() as conn:
        value = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1",
                                    tenant_id)
    return str(value or "")


def _private_kb() -> dict[str, Any]:
    """Нижняя клавиатура лички. Кнопка мини-аппа появляется, когда он развёрнут."""
    return keyboards.persistent_private(miniapp.web_app_url())


async def _open_app(packed: str, tg_user_id: int) -> Reply:
    """Открыть мини-апп в контексте чата, из которого пришли по ссылке.

    Так работает переход из группы, когда у бота не заведено короткое имя
    приложения: кнопку `web_app` Telegram разрешает только в личке, поэтому
    ссылка из группы ведёт сюда, а кнопку с контекстом мы даём уже здесь.
    """
    packed_ctx = miniapp.unpack_context(packed)
    if packed_ctx is None:
        return Reply(texts.MSG_DIALOG_EXPIRED, markup=_private_kb())

    ctx = await load_chat_context_by_ref(packed_ctx.chat_ref, packed_ctx.thread_id)
    if ctx is None or ctx.tenant_id is None:
        return Reply(texts.MSG_DIALOG_EXPIRED, markup=_private_kb())
    if await access.linked_b24_user(ctx.tenant_id, tg_user_id) is None:
        return Reply(texts.MSG_NOT_LINKED, markup=_private_kb())

    url = miniapp.web_app_url_for(packed)
    if url is None:
        return Reply(texts.MSG_APP_UNAVAILABLE, markup=_private_kb())

    title = esc_html(ctx.title or "чат")
    return Reply(
        f"Задачи чата «{title}».\nОткройте приложение — там фильтры, срок, "
        f"ответственный и комментарии.",
        markup=keyboards.inline([[keyboards.web_app_button("🧩 Открыть задачи", url)]]))


async def _ensure_menu_button(bot: dict[str, Any], tg_user_id: int) -> None:
    """Повесить мини-апп на кнопку меню В ЭТОЙ личке.

    Умолчание для всех чатов Telegram принимает, но не показывает, если у бота
    настроено меню команд (проверено на живом боте). Адресная установка работает,
    стоит один вызов и делается там, где человек и так пришёл в личку.
    """
    url = miniapp.web_app_url()
    if url is None:
        return
    try:
        await tg_api.set_chat_menu_button(bot["token"], url, chat_id=tg_user_id)
    except tg_api.TelegramError as exc:
        # Кнопка — удобство, а не условие работы: молча продолжаем.
        log.info("кнопка меню не поставлена для %s: %s", tg_user_id, exc)


async def _private(bot: dict[str, Any], cmd: tuple[str, str] | None,
                   tg_user_id: int, user: dict[str, Any],
                   text: str = "") -> Reply | None:
    if cmd is None:
        # Постоянная клавиатура шлёт обычный ТЕКСТ, а не callback. Без разбора
        # подписей любое нажатие выглядело как «бот не реагирует».
        action = keyboards.PRIVATE_LABELS.get(text.strip())
        if action:
            return await _private_action(action, tg_user_id)
        return Reply(_help_text(private=True), markup=_private_kb())
    name, arg = cmd

    if name == "start" and arg.startswith("b"):
        reply = await _link_account(arg[1:], tg_user_id, user)
        await _ensure_menu_button(bot, tg_user_id)
        return Reply(reply.text, markup=_private_kb())
    if name == "start" and arg.startswith(miniapp.START_PREFIX) and len(arg) > 1:
        await _ensure_menu_button(bot, tg_user_id)
        return await _open_app(arg[1:], tg_user_id)
    if name in ("start", "help"):
        # Кнопка меню ставится адресно именно здесь: личный чат уже известен,
        # а без chat_id Telegram принимает вызов и кнопку не показывает.
        await _ensure_menu_button(bot, tg_user_id)
        return Reply(_help_text(private=True), markup=_private_kb())
    if name == "whoami":
        return await _whoami(tg_user_id)
    if name == "link":
        return Reply(texts.MSG_NOT_LINKED, markup=_private_kb())
    # Те же действия, что на постоянной клавиатуре: человек, привыкший к слешам,
    # не должен искать кнопку, а пришедший из меню Telegram — знать про кнопки.
    slash_actions = {"status": "mine", "list": "mine",
                     "overdue": "overdue", "mychats": "mychats",
                     "pending": "pending"}
    if name in slash_actions:
        return await _private_action(slash_actions[name], tg_user_id)
    return Reply(_help_text(private=True), markup=_private_kb())


async def _private_action(action: str, tg_user_id: int) -> Reply:
    """Действия постоянной клавиатуры.

    В личке нет контекста чата, поэтому собираем задачи по ВСЕМ проектам теннанта,
    привязанным хоть к одному чату. Видно при этом ровно то, что видит сам человек:
    ходим его личным токеном.
    """
    if action == "help":
        return Reply(_help_text(private=True), markup=_private_kb())

    tenant_id = await access.tenant_of_user(tg_user_id)
    if tenant_id is None:
        return Reply(texts.MSG_NOT_LINKED, markup=_private_kb())
    b24_user_id = await access.linked_b24_user(tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED, markup=_private_kb())

    if action == "pending":
        return await _pending_approvals(tenant_id, tg_user_id)

    if action == "mychats":
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.title, p.name AS project, cl.name AS client
                  FROM chat_bindings b
                  JOIN tg_chats c ON c.id = b.chat_ref
                  JOIN projects p ON p.id = b.project_id
                  JOIN clients cl ON cl.id = p.client_id
                 WHERE b.tenant_id = $1 AND b.status = 'active'
                 ORDER BY c.title, p.name
                """, tenant_id)
        if not rows:
            return Reply("Пока ни один чат не привязан к проекту.",
                         markup=_private_kb())
        lines = ["<b>Привязанные чаты</b>", ""]
        for r in rows:
            lines.append(f"• {esc_html(r['title'] or 'без названия')}")
            lines.append(f"    {esc_html(r['client'])} · {esc_html(r['project'])}")
        return Reply("\n".join(lines), markup=_private_kb())

    async with pool().acquire() as conn:
        groups = await conn.fetch(
            "SELECT DISTINCT p.b24_group_id FROM chat_bindings b "
            "JOIN projects p ON p.id = b.project_id AND p.status = 'active' "
            "WHERE b.tenant_id = $1 AND b.status = 'active'", tenant_id)
    group_ids = [int(g["b24_group_id"]) for g in groups]
    if not group_ids:
        return Reply(texts.MSG_NO_PROJECT, markup=_private_kb())

    try:
        client = await access.client_for_user(tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            tasks = await views.fetch_open(client, group_ids)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH, markup=_private_kb())
    except errors.B24Error:
        return Reply(texts.MSG_B24_UNAVAILABLE, markup=_private_kb())

    if action == "overdue":
        tasks = [t for t in tasks if views.is_overdue(t)]
        title = "🔥 Просроченные"
    else:
        tasks = [t for t in tasks if str(t.get("responsibleId")) == str(b24_user_id)]
        title = "📊 Мои задачи"
    return Reply(views.render_list(tasks, title=title),
                 markup=_private_kb())


# ------------------------------------------------------------ подтверждение задач
APPROVAL_PAGE = 15  # столько поместится кнопок под одним сообщением


async def _pending_approvals(tenant_id: int, tg_user_id: int) -> Reply:
    """/pending и кнопка «🙋 Ожидают подтверждения» — личный список, не чата.

    Как и остальные действия постоянной клавиатуры, собирает задачи по всему
    теннанту: у решения ответственного нет привязки к одному чату.
    """
    user_id = await approvals.user_id_of(tg_user_id)
    if user_id is None:
        return Reply(texts.MSG_APPROVAL_PENDING_EMPTY, markup=_private_kb())

    items, total = await approvals.pending_for(tenant_id, user_id, limit=APPROVAL_PAGE)
    if not items:
        return Reply(texts.MSG_APPROVAL_PENDING_EMPTY, markup=_private_kb())

    lines = ["<b>🙋 Ожидают вашего подтверждения</b>", ""]
    buttons: list[list[dict[str, str]]] = []
    for item in items:
        lines.append(f"#{item.b24_task_id} · {esc_html(item.task_title)}")
        lines.append(f"    {esc_html(item.client_name)} · {esc_html(item.project_name)}")
        confirm = await issue_token(
            "task_approval", tenant_id=tenant_id, owner_tg_id=tg_user_id,
            payload={"approval_id": item.id, "decision": "confirm"}, ttl=timedelta(days=30))
        reject = await issue_token(
            "task_approval", tenant_id=tenant_id, owner_tg_id=tg_user_id,
            payload={"approval_id": item.id, "decision": "reject"}, ttl=timedelta(days=30))
        buttons.append([
            {"text": f"✅ #{item.b24_task_id}", "callback_data": f"av:{confirm}"},
            {"text": f"❌ #{item.b24_task_id}", "callback_data": f"av:{reject}"},
        ])

    text = "\n".join(lines)
    if total > len(items):
        # Молчаливое усечение — тот же баг продукта, что и везде в этом боте.
        text += f"\n\nПоказаны первые {len(items)} из {total}."
    return Reply(text, buttons=buttons)


async def _approval_vote(tenant_id: int, tg_user_id: int, payload: dict[str, Any]) -> Reply:
    """Нажатие «Подтвердить»/«Отклонить» под запросом в личке."""
    decision_raw = str(payload.get("decision") or "")
    if decision_raw not in ("confirm", "reject") or not payload.get("approval_id"):
        return Reply(texts.MSG_DIALOG_EXPIRED, markup={"inline_keyboard": []}, edit=True)
    decision: approvals.Decision = "confirm" if decision_raw == "confirm" else "reject"

    result = await approvals.resolve(tenant_id, int(payload["approval_id"]), decision,
                                     tg_user_id)

    if result.outcome == "confirmed":
        text = texts.MSG_APPROVAL_CONFIRMED.format(
            task_id=result.task_id, title=esc_html(result.title),
            stage=esc_html(result.stage_title))
    elif result.outcome == "rejected":
        text = texts.MSG_APPROVAL_REJECTED.format(
            task_id=result.task_id, title=esc_html(result.title),
            stage=esc_html(result.stage_title))
    elif result.outcome == "already_done":
        text = texts.MSG_APPROVAL_ALREADY_DONE.format(
            task_id=result.task_id, title=esc_html(result.title))
    elif result.outcome == "needs_reauth":
        text = texts.MSG_NEEDS_REAUTH
    elif result.outcome == "b24_error":
        text = texts.MSG_B24_UNAVAILABLE
    else:  # forbidden, not_found — чужое или устаревшее нажатие
        text = texts.MSG_DIALOG_EXPIRED
    # Кнопки снимаются в любом исходе: повторное нажатие по тому же сообщению
    # либо уже невозможно (токен одноразовый), либо бессмысленно.
    return Reply(text, markup={"inline_keyboard": []}, edit=True)


async def _link_account(token: str, tg_user_id: int, user: dict[str, Any]) -> Reply:
    """Завершение привязки: человек открыл приложение в Б24 и перешёл по deep link."""
    row = await consume_token(token, None)
    if row is None or row["kind"] != "link":
        return Reply(texts.MSG_LINK_BAD_TOKEN)

    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    tenant_id, b24_user_id = int(row["tenant_id"]), int(payload["b24_user_id"])

    async with pool().acquire() as conn, conn.transaction():
        user_row = await conn.fetchrow(
            "INSERT INTO users (tg_user_id, tg_username, display_name) VALUES ($1,$2,$3) "
            "ON CONFLICT (tg_user_id) DO UPDATE SET tg_username = EXCLUDED.tg_username, "
            "display_name = EXCLUDED.display_name RETURNING id",
            tg_user_id, user.get("username"), _author(user))
        await conn.execute(
            "INSERT INTO tenant_members (tenant_id, user_id, role, b24_user_id, "
            "link_status, linked_at) VALUES ($1,$2,'member',$3,'authorized',now()) "
            "ON CONFLICT (tenant_id, user_id) DO UPDATE SET b24_user_id = EXCLUDED.b24_user_id, "
            "link_status = 'authorized', linked_at = now()",
            tenant_id, user_row["id"], b24_user_id)
        # Токен отдаётся только тому, кто его авторизовал (docs/40-security.md §2).
        await conn.execute(
            "UPDATE b24_user_tokens SET authorized_tg_user_id = $3 "
            "WHERE tenant_id = $1 AND b24_user_id = $2", tenant_id, b24_user_id, tg_user_id)

    log.info("привязка завершена: теннант %s, Б24 %s, TG %s",
             tenant_id, b24_user_id, tg_user_id)
    return Reply(texts.MSG_LINK_DONE)


async def _whoami(tg_user_id: int) -> Reply:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT t.name, m.role, m.b24_user_id, m.link_status FROM tenant_members m "
            "JOIN users u ON u.id = m.user_id JOIN tenants t ON t.id = m.tenant_id "
            "WHERE u.tg_user_id = $1", tg_user_id)
    if row is None:
        return Reply(texts.MSG_NOT_LINKED)
    return Reply(f"Теннант: <b>{esc_html(row['name'])}</b>\n"
                 f"Пользователь Битрикс24: <code>{row['b24_user_id']}</code>\n"
                 f"Состояние привязки: {esc_html(row['link_status'])}")


async def _group_command(bot: dict[str, Any], ctx: ChatContext, cmd: tuple[str, str],
                         msg: dict[str, Any], tg_user_id: int,
                         user: dict[str, Any]) -> Reply | None:
    name, arg = cmd

    if name in ("start", "help"):
        return await _help_reply(ctx, tg_user_id)
    if name in ("status", "list", "overdue"):
        if not ctx.is_active or not ctx.has_binding:
            return Reply(texts.MSG_NO_PROJECT)
        action = {"status": "status", "list": "all", "overdue": "overdue"}[name]
        return await _open_summary(ctx, tg_user_id, action)
    if name == "whoami":
        return await _whoami(tg_user_id)
    if name == "link":
        return Reply(texts.MSG_NOT_LINKED)

    if name in ("bind", "bindings", "unbind"):
        return await _bind_commands(ctx, name, tg_user_id)


    if not ctx.is_active:
        return Reply(texts.MSG_NOT_CLAIMED)
    if not ctx.has_binding:
        return Reply(texts.MSG_NO_PROJECT)

    if name in ("task", "ask"):
        # Порядок ключей важен: {"text": arg, **msg} затирает arg исходным текстом,
        # и команда «/task» уезжает в заголовок задачи. Проверено на живой задаче.
        source = msg.get("reply_to_message")
        if source is None and arg and name == "task":
            source = {**msg, "text": arg, "caption": None}
        if source is None:
            # Ни реплая, ни текста — значит человек не знает, что писать.
            # Именно для этого и существует опросник.
            return await _survey_start(ctx, tg_user_id)
        return await _create_from(ctx, source, tg_user_id, msg.get("message_id"))
    if name == "comment":
        return await _comment_command(ctx, msg, arg, tg_user_id)
    if name == "discussion":
        return await _discussion(ctx, arg, tg_user_id)
    if name == "cancel":
        session = await survey.active_for(ctx.chat_ref, msg.get("message_thread_id"),
                                          tg_user_id)
        if session is None:
            return None
        await survey.finish(session.id, "cancelled")
        return Reply(texts.MSG_SURVEY_CANCELLED)
    return None


async def _bind_commands(ctx: ChatContext, name: str, tg_user_id: int) -> Reply:
    if name == "bindings":
        if not ctx.projects:
            return Reply(texts.MSG_BINDINGS_EMPTY)
        lines = [f"• <b>{esc_html(p.name)}</b> (клиент {esc_html(p.client_name)})"
                 for p in ctx.projects]
        return Reply("К этому чату привязаны:\n" + "\n".join(lines))

    # Чат может быть ещё ничьим — это нормально: принадлежность и возникает при
    # привязке. Теннанта берём у того, кто команду выполняет.
    tenant_id = ctx.tenant_id or await access.tenant_of_user(tg_user_id)
    if tenant_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    b24_user_id = await access.linked_b24_user(tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    # Матрица прав (docs/40-security.md §3): привязка чата — действие админа теннанта.
    # Привязка решает, чьи задачи видны в чате, поэтому «любой сопоставленный» здесь
    # слишком широко: сотрудник клиента тоже сопоставлен.
    if not await access.is_tenant_admin(tenant_id, tg_user_id):
        return Reply(texts.MSG_NEED_TENANT_ADMIN)

    if name == "unbind":
        # Отвязывать можно только то, что привязано.
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                "SELECT p.id, p.name, c.name AS client_name FROM chat_bindings b "
                "JOIN projects p ON p.id = b.project_id "
                "JOIN clients c ON c.id = p.client_id "
                "WHERE b.chat_ref = $1 AND b.status = 'active'", ctx.chat_ref)
        if not rows:
            return Reply(texts.MSG_BINDINGS_EMPTY)
        buttons = []
        for r in rows:
            token = await issue_token("admin:bind", tenant_id=tenant_id,
                                      owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                                      payload={"project_id": r["id"], "action": "unbind"})
            buttons.append([{"text": f"{r['client_name']} · {r['name']}",
                             "callback_data": f"b:{token}"}])
        return Reply("Какую привязку снять?", buttons=buttons)

    # Список берём С ПОРТАЛА, а не из своей таблицы: показывать только уже
    # импортированные проекты — значит показывать три штуки из четырнадцати
    # и оставлять человека гадать, по какому принципу.
    try:
        client = await access.client_for_user(tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            groups = await client.call("sonet_group.user.groups", {})
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24Error as exc:
        log.warning("не удалось получить проекты портала: %s", exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    async with pool().acquire() as conn:
        bound = await conn.fetch(
            "SELECT p.b24_group_id FROM chat_bindings b "
            "JOIN projects p ON p.id = b.project_id "
            "WHERE b.chat_ref = $1 AND b.status = 'active'", ctx.chat_ref)
    already = {int(r["b24_group_id"]) for r in bound}

    items = []
    for g in groups or []:
        gid = g.get("GROUP_ID") or g.get("ID")
        if gid is None or int(gid) in already:
            continue
        items.append((int(gid), str(g.get("GROUP_NAME") or g.get("NAME") or gid)))
    items.sort(key=lambda x: x[1].lower())

    if not items:
        return Reply(texts.MSG_BIND_ALL_BOUND)

    buttons = []
    for gid, title in items[:BIND_PAGE]:
        token = await issue_token("admin:bind", tenant_id=tenant_id,
                                  owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                                  payload={"b24_group_id": gid, "name": title,
                                           "action": "bind"})
        buttons.append([{"text": title[:60], "callback_data": f"b:{token}"}])

    head = texts.MSG_BIND_CHOOSE
    if len(items) > BIND_PAGE:
        # Молчаливое усечение — ровно та жалоба, с которой начался этот код:
        # «показывает не все проекты, непонятно по какому принципу».
        head += texts.MSG_BIND_TRUNCATED.format(shown=BIND_PAGE, total=len(items))
    return Reply(head, buttons=buttons)


async def _create_from(ctx: ChatContext, source: dict[str, Any], tg_user_id: int,
                       trigger_message_id: int | None) -> Reply:
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)

    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    text = _text_of(source)
    if not text.strip():
        return Reply(texts.MSG_EMPTY_SOURCE)

    if len(ctx.projects) > 1:
        buttons = []
        for p in ctx.projects:
            token = await issue_token("task:project", tenant_id=ctx.tenant_id,
                                      owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                                      payload={"project_id": p.id,
                                               "source_message_id": source.get("message_id")})
            buttons.append([{"text": f"{p.client_name} · {p.name}",
                             "callback_data": f"p:{token}"}])
        return Reply(texts.MSG_CHOOSE_PROJECT, buttons=buttons)

    return await _do_create(ctx, ctx.projects[0], source, tg_user_id, b24_user_id)


async def _do_create(ctx: ChatContext, project: ProjectRef, source: dict[str, Any],
                     tg_user_id: int, b24_user_id: int) -> Reply:
    assert ctx.tenant_id is not None
    source_id = int(source.get("message_id") or 0)
    draft = task_create.extract(
        _text_of(source),
        author=_author(source.get("from") or {}),
        chat_title=ctx.title,
        message_link=task_create.message_link(ctx.chat_id, source_id),
        idem_key=f"tgsrc-{ctx.chat_ref}-{source_id}",
        source_message_id=source_id)

    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                             actor_tg_user_id=tg_user_id)
        async with client:
            task, created = await task_create.create(
                client, ctx.tenant_id, project, draft, b24_user_id)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24AccessDenied as exc:
        return Reply(texts.MSG_NO_RIGHTS_B24.format(reason=esc_html(exc.description)))
    except errors.B24Error as exc:
        log.warning("создание задачи не удалось: %s", exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    if not created:
        return Reply(texts.MSG_TASK_EXISTS.format(
            task_id=task.get("id"), title=esc_html(task.get("title") or "")))

    async with pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO tg_message_links (tenant_id, chat_ref, message_id, kind, "
            "b24_task_id) VALUES ($1,$2,$3,'source',$4) ON CONFLICT DO NOTHING",
            ctx.tenant_id, ctx.chat_ref, source_id, int(task["id"]))

    # Файлы из исходного сообщения переносим в задачу.
    files_note = await _transfer(ctx, source, int(task["id"]), project, b24_user_id,
                                 tg_user_id, draft.idem_key)

    responsible = task.get("responsible") or {}
    text = texts.MSG_TASK_CREATED.format(
        task_id=task.get("id"), title=esc_html(task.get("title") or ""),
        project=esc_html(project.name),
        responsible=esc_html(responsible.get("name") or b24_user_id))
    return Reply(text + files_note)


async def _transfer(ctx: ChatContext, source: dict[str, Any], task_id: int,
                    project: ProjectRef, b24_user_id: int, tg_user_id: int,
                    idem_key: str) -> str:
    """Перенос вложений. Возвращает приписку к ответу — или пустую строку."""
    attachments = tg_files.extract(source)
    if not attachments or ctx.tenant_id is None:
        return ""

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT b.token, b.bot_id, b.tenant_id FROM tg_bots b "
            "JOIN tg_chats c ON c.bot_ref = b.id WHERE c.id = $1", ctx.chat_ref)
    if row is None:
        return ""
    bot_token = box.decrypt(row["token"],
                            box.aad("tg_bots", "token", row["tenant_id"], row["bot_id"]))

    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            count, rejected = await comments.transfer_files(
                client, bot_token, ctx.tenant_id, task_id, project.b24_group_id,
                b24_user_id, attachments, idem_key)
    except errors.B24Error as exc:
        log.warning("перенос вложений не удался: %s", exc)
        return "\n\n<i>Файлы перенести не удалось.</i>"

    parts = []
    if count:
        parts.append(f"Файлов приложено: {count}")
    for reason in rejected:
        parts.append(esc_html(reason))
    return ("\n\n<i>" + "; ".join(parts) + "</i>") if parts else ""


# --------------------------------------------------------------------- кнопки
async def on_callback(bot: dict[str, Any], cb: dict[str, Any]) -> Reply | None:
    data = str(cb.get("data") or "")
    user = cb.get("from") or {}
    tg_user_id = int(user.get("id") or 0)
    msg = cb.get("message") or {}
    chat_id = int((msg.get("chat") or {}).get("id") or 0)

    ns, _, token = data.partition(":")
    row = await consume_token(token, tg_user_id)
    if row is None:
        return Reply(texts.MSG_DIALOG_EXPIRED)

    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]

    if ns == "av":
        # Кнопки подтверждения задачи живут в личке ответственного, а не в чате
        # клиента: личные чаты в tg_chats не регистрируются (dispatch.py), поэтому
        # ChatContext здесь взять неоткуда — это нормальное свойство личного
        # диалога, а не повод отвечать «чат не подключён».
        if row["tenant_id"] is None:
            return Reply(texts.MSG_DIALOG_EXPIRED)
        return await _approval_vote(int(row["tenant_id"]), tg_user_id, payload)

    ctx = await load_chat_context(chat_id, msg.get("message_thread_id"))
    if ctx is None:
        return Reply(texts.MSG_NOT_CLAIMED)

    # Теннант берётся из токена: он записан в момент выдачи кнопки и не зависит от
    # того, стал ли чат к этому моменту чьим-то.
    tenant_id = int(row["tenant_id"]) if row["tenant_id"] is not None else ctx.tenant_id
    if tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)

    if ns == "s":
        return await _survey_begin(ctx, tg_user_id, int(payload["template_id"]))
    if ns == "k":
        session = await survey.active_for(ctx.chat_ref, msg.get("message_thread_id"),
                                          tg_user_id)
        if session is None:
            return Reply(texts.MSG_DIALOG_EXPIRED)
        items = await survey.questions(tenant_id, session.template_id)
        await survey.skip(session)
        return await _survey_next(ctx, session, items, tg_user_id)
    if ns == "p":
        session = await survey.active_for(ctx.chat_ref, msg.get("message_thread_id"),
                                          tg_user_id)
        if session is None:
            return Reply(texts.MSG_DIALOG_EXPIRED)
        items = await survey.questions(tenant_id, session.template_id)
        if session.step >= len(items):
            return Reply(texts.MSG_DIALOG_EXPIRED)
        q = items[session.step]
        index = int(payload.get("index", -1))
        if not (0 <= index < len(q.options)):
            # Набор правили, пока человек думал над кнопкой.
            return Reply(texts.MSG_DIALOG_EXPIRED)
        option = q.options[index]
        await survey.record(session, q.code,
                            str(option.get("value") or option.get("label") or ""))
        return await _survey_next(ctx, session, items, tg_user_id)
    if ns == "x":
        await survey.finish(int(payload["session_id"]), "cancelled")
        return Reply(texts.MSG_SURVEY_CANCELLED)
    if ns == "c":
        return await _survey_create(ctx, tg_user_id, int(payload["session_id"]),
                                    int(payload["project_id"]))
    if ns == "m":
        action = str(payload.get("action") or "status")
        if action == "new":
            return await _survey_start(ctx, tg_user_id)
        return await _open_summary(ctx, tg_user_id, action)
    if ns == "t":
        return await _open_card(ctx, tg_user_id, int(payload["task_id"]))
    if ns == "a":
        return await _task_action(ctx, tg_user_id, int(payload["task_id"]),
                                  str(payload.get("act") or "refresh"))
    if ns == "e":
        return await _edit(ctx, tg_user_id, payload)
    if ns == "b":
        return await _apply_bind(ctx, tenant_id, payload, tg_user_id)
    if ns == "p":
        b24_user_id = await access.linked_b24_user(tenant_id, tg_user_id)
        if b24_user_id is None:
            return Reply(texts.MSG_NOT_LINKED)
        project = next((p for p in ctx.projects if p.id == payload["project_id"]), None)
        if project is None:
            async with pool().acquire() as conn:
                r = await conn.fetchrow(
                    "SELECT p.id, p.b24_group_id, p.name, c.name AS client_name "
                    "FROM projects p JOIN clients c ON c.id = p.client_id WHERE p.id = $1",
                    payload["project_id"])
            if r is None:
                return Reply(texts.MSG_DIALOG_EXPIRED)
            project = ProjectRef(r["id"], r["b24_group_id"], r["name"], r["client_name"])
        source = {"message_id": payload.get("source_message_id"),
                  "text": payload.get("text", ""), "from": user}
        return await _do_create(ctx, project, source, tg_user_id, b24_user_id)
    return None


async def _apply_bind(ctx: ChatContext, tenant_id: int, payload: dict[str, Any],
                      tg_user_id: int) -> Reply:
    action = payload.get("action", "bind")

    # Роль проверяется в момент действия, а не в момент выдачи кнопки: за время
    # жизни клавиатуры человека могли понизить (docs/40-security.md §3).
    if not await access.is_tenant_admin(tenant_id, tg_user_id):
        return Reply(texts.MSG_NEED_TENANT_ADMIN)

    if "b24_group_id" in payload:
        # Проект выбран с портала — импортируем его при первой привязке.
        b24_user_id = await access.linked_b24_user(tenant_id, tg_user_id)
        if b24_user_id is None:
            return Reply(texts.MSG_NOT_LINKED)
        project_id = await _import_project(
            tenant_id, b24_user_id, int(payload["b24_group_id"]),
            str(payload.get("name") or ""))
        if project_id is None:
            return Reply(texts.MSG_B24_UNAVAILABLE)
    else:
        project_id = int(payload["project_id"])

    async with pool().acquire() as conn:
        proj = await conn.fetchrow(
            "SELECT p.name, c.name AS client_name FROM projects p "
            "JOIN clients c ON c.id = p.client_id WHERE p.id = $1", project_id)
    if proj is None:
        return Reply(texts.MSG_DIALOG_EXPIRED)

    if action == "unbind":
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE chat_bindings SET status = 'disabled' "
                "WHERE chat_ref = $1 AND project_id = $2", ctx.chat_ref, project_id)
        await audit.record(tenant_id, "chat.unbind", actor_tg_id=tg_user_id,
                           project_id=project_id, target=f"chat:{ctx.chat_ref}",
                           detail={"проект": proj["name"], "чат": ctx.title})
        return Reply(texts.MSG_UNBOUND)

    async with pool().acquire() as conn:
        # Чужой чат перехватить нельзя: если он уже принадлежит другому теннанту,
        # привязка не выполняется.
        owner = await conn.fetchval("SELECT tenant_id FROM tg_chats WHERE id = $1",
                                    ctx.chat_ref)
        if owner is not None and int(owner) != tenant_id:
            log.warning("попытка привязать чужой чат %s: владелец %s, просят %s",
                        ctx.chat_ref, owner, tenant_id)
            return Reply(texts.MSG_BIND_CONFLICT)

        try:
            await conn.execute(
                "INSERT INTO chat_bindings (tenant_id, chat_ref, project_id, status) "
                "VALUES ($1,$2,$3,'active') "
                "ON CONFLICT (chat_ref, COALESCE(topic_ref, 0), project_id) "
                "DO UPDATE SET status = 'active'",
                tenant_id, ctx.chat_ref, project_id)
        except Exception as exc:  # триггер «один чат — один клиент»
            if "another client" in str(exc):
                return Reply(texts.MSG_BIND_CONFLICT)
            raise

        # Чат перестаёт быть ничейным: теперь он обслуживает конкретного клиента.
        await conn.execute(
            "UPDATE tg_chats SET tenant_id = $2, status = 'active', "
            "claimed_at = COALESCE(claimed_at, now()) WHERE id = $1",
            ctx.chat_ref, tenant_id)

    await audit.record(tenant_id, "chat.bind", actor_tg_id=tg_user_id,
                       project_id=project_id, target=f"chat:{ctx.chat_ref}",
                       detail={"проект": proj["name"], "клиент": proj["client_name"],
                               "чат": ctx.title})
    log.info("чат %s привязан к проекту %s пользователем TG %s",
             ctx.chat_ref, project_id, tg_user_id)
    return Reply(texts.MSG_BIND_DONE.format(
        project=esc_html(proj["name"]), client=esc_html(proj["client_name"])))
