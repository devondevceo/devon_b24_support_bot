"""Сценарии бота: команды, создание задач, кнопки."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from b24bot.b24 import errors, mapping
from b24bot.b24.limiter import Lane
from b24bot.b24.tokens import NeedsReauth
from b24bot.bot import (
    callbacks,
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
from b24bot.domain import (
    access,
    approvals,
    audit,
    dm,
    linking,
    miniapp,
    reminders,
    sync,
    timelog,
    timesheet,
)
from b24bot.domain import events as b24_events
from b24bot.domain import tasks as task_service
from b24bot.domain.context import (
    ChatContext,
    ProjectRef,
    authorize_task_for_chat,
    authorize_task_for_tenant,
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
        return await _private(bot, cmd, tg_user_id, user, text, msg)

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
        # Ответ на приглашение «✏️ Другое» — вторая и последняя форма свободного
        # ввода в группе. Тоже строго реплаем и тоже на своё же сообщение.
        logged = await _timelog_answer(ctx, msg, reply_to, tg_user_id, bot)
        if logged is not None:
            return logged

    # Реплай с упоминанием бота — основной триггер создания задачи.
    if reply_to and _mentions_bot(msg, bot_username):
        return await _create_from(ctx, reply_to, tg_user_id, msg.get("message_id"))
    return None


def _task_number(arg: str) -> int | None:
    """Номер задачи из аргумента команды или из ссылки на портал."""
    text = arg.strip()
    match = re.search(r"/task/view/(\d+)", text) or re.match(r"#?(\d+)", text)
    return int(match.group(1)) if match else None


@dataclass
class CommentInput:
    """Что именно уйдёт в комментарий задачи."""

    task_id: int | None
    text: str
    quoted_author: str  # пусто, когда текст набрал сам отправитель команды
    attachments: list[tg_files.Attachment]


def comment_input(arg: str, msg: dict[str, Any]) -> CommentInput:
    """Разбор `/comment`: свой текст, а если его нет — текст сообщения, на которое
    ответили.

    Ответить «/comment 233» на чужую реплику — обычный жест в переписке, и он
    избавляет от переписывания этой реплики руками. Файлы из процитированного
    сообщения едут вместе с текстом: разделять их означало бы терять половину
    смысла сообщения без единого слова об этом.
    """
    text = arg.split(" ", 1)[1].strip() if " " in arg else ""
    reply_to = msg.get("reply_to_message") or {}
    quoted_author = ""
    attachments = tg_files.extract(msg)

    if not text and reply_to:
        quoted_text = _text_of(reply_to).strip()
        quoted_files = tg_files.extract(reply_to)
        # Пустой реплай — это не цитата. В форуме Telegram сам подставляет ответ
        # на служебное сообщение о создании топика: сославшись на него, мы бы
        # приписали комментарий тому, кто завёл топик, и ничего не сказали бы
        # о содержимом.
        if quoted_text or quoted_files:
            text = quoted_text
            quoted_author = _author(reply_to.get("from") or {})
            seen = {a.file_id for a in attachments}
            attachments += [a for a in quoted_files if a.file_id not in seen]
    return CommentInput(_task_number(arg), text, quoted_author, attachments)


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


@dataclass(frozen=True)
class TimeInput:
    task_id: int | None
    seconds: int | None
    comment: str
    quoted_author: str
    error: str = ""


def time_input(arg: str, msg: dict[str, Any]) -> TimeInput:
    """Разбор `/time <номер> <длительность> [комментарий]`.

    Чистая функция: то же решение, что у `comment_input`, и по той же причине —
    разбор аргументов проверяется тестом без портала, без базы и без Telegram.

    Комментарий берётся из реплая, если его не написали в самой команде. Жест тот
    же, что у `/comment`, и он избавляет от переписывания чужой реплики руками.
    Пустой реплай цитатой не считается: в форуме Telegram сам подставляет ответ
    на служебное сообщение о создании топика.
    """
    # `arg` — это уже ТОЛЬКО аргументы: имя команды отрезает `_command`.
    parts = arg.split()
    task_id = _task_number(arg)
    if task_id is None or len(parts) < 2:
        return TimeInput(task_id, None, "", "", error="usage")

    try:
        seconds = timelog.parse_duration(parts[1])
    except timelog.BadDuration as exc:
        return TimeInput(task_id, None, "", "", error=str(exc))

    comment = arg.split(None, 2)[2].strip() if len(parts) > 2 else ""
    quoted_author = ""
    if not comment:
        reply_to = msg.get("reply_to_message") or {}
        quoted = _text_of(reply_to).strip() if reply_to else ""
        if quoted:
            comment = quoted
            quoted_author = _author(reply_to.get("from") or {})
    return TimeInput(task_id, seconds, comment, quoted_author)


async def _time_command(ctx: ChatContext, msg: dict[str, Any], arg: str,
                        tg_user_id: int) -> Reply:
    """`/time` — списание времени в задачу.

    Без номера отвечает списком задач с кнопкой под каждой, с номером без
    длительности — экраном этой задачи, полной формой — сразу списывает.
    """
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    data = time_input(arg, msg)
    if data.task_id is None:
        # `/time` без номера — не ошибка, а самый частый способ им пользоваться:
        # номер задачи наизусть не помнит никто. Отвечаем списком с кнопкой
        # списания под каждой задачей.
        return await _timelog_pick(ctx, tg_user_id, b24_user_id)
    if data.seconds is None and data.error == "usage":
        # Номер назвали, длительность нет — открываем экран задачи с быстрыми
        # длительностями, а не выговариваем формат: спросили ровно про эту задачу.
        return await _timelog(ctx, tg_user_id,
                              {"task_id": data.task_id, "act": "menu"})
    if data.seconds is None:
        return Reply(texts.MSG_TIMELOG_BAD_DURATION.format(
            reason=esc_html(data.error)) + "\n\n" + texts.MSG_TIMELOG_USAGE)

    project = await _authorize_live(ctx, data.task_id, b24_user_id, tg_user_id)
    if project is None:
        return Reply(texts.MSG_TASK_NOT_FOUND)

    # Автор цитаты называется в самом комментарии: без этого списание выглядит
    # так, будто нажавший пересказал чужие слова от своего имени.
    comment = data.comment
    if data.quoted_author:
        comment = f"{comment} (из Telegram, автор: {data.quoted_author})"

    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            await timelog.add(client, data.task_id, data.seconds, comment=comment)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24AccessDenied as exc:
        return Reply(texts.MSG_NO_RIGHTS_B24.format(reason=esc_html(exc.description)))
    except errors.B24Error as exc:
        log.warning("списание времени в задачу %s не удалось: %s", data.task_id, exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    await _audit_timelog(ctx.tenant_id, b24_user_id, tg_user_id, data.task_id,
                         project.id, data.seconds, source="bot")

    domain = await _tenant_domain(ctx.tenant_id)
    ref = views.task_ref(data.task_id, domain=domain, b24_user_id=b24_user_id)
    return Reply(f"⏱ В задачу {ref} списано "
                 f"{timelog.format_duration(data.seconds)}.")


async def _comment_command(ctx: ChatContext, msg: dict[str, Any], arg: str,
                           tg_user_id: int) -> Reply:
    """/comment <номер> [текст] — комментарий в задачу.

    Без текста команда работает реплаем: в задачу уходит сообщение, на которое
    ответили, вместе с его файлами.
    """
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    data = comment_input(arg, msg)
    task_id = data.task_id
    if task_id is None or not (data.text or data.attachments):
        return Reply(texts.MSG_COMMENT_USAGE)

    project = await _authorize_live(ctx, task_id, b24_user_id, tg_user_id)
    if project is None:
        return Reply(texts.MSG_TASK_NOT_FOUND)

    author = _author(msg.get("from") or {})
    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            # Сообщение без текста, но с файлами — не пустой комментарий: в задаче
            # должна остаться строка о том, откуда взялись вложения.
            await comments.add(client, task_id, data.text or texts.MSG_COMMENT_FILES,
                               author=author, chat_title=ctx.title,
                               quoted_from=data.quoted_author)
            note = ""
            if data.attachments:
                count, rejected = await comments.transfer_files(
                    client, await _bot_token(ctx), ctx.tenant_id, task_id,
                    project.b24_group_id, b24_user_id, data.attachments,
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

    domain = await _tenant_domain(ctx.tenant_id)
    ref = views.task_ref(task_id, domain=domain, b24_user_id=b24_user_id)
    if data.quoted_author:
        note = texts.MSG_COMMENT_QUOTED.format(
            author=esc_html(data.quoted_author)) + note
    return Reply(texts.MSG_COMMENT_ADDED.format(task=ref) + note)


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
    """/discussion <номер>. Номер можно дать числом или ссылкой на портал."""
    task_id = _task_number(arg)
    if task_id is None:
        return Reply(texts.MSG_DISCUSSION_USAGE)
    return await _discussion_for(ctx, task_id, tg_user_id)


async def _discussion_for(ctx: ChatContext, task_id: int, tg_user_id: int) -> Reply:
    """Показать обсуждение задачи. Комментарии лежат в чате задачи, не в форуме."""
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

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

    domain = await _tenant_domain(ctx.tenant_id)
    ref = views.task_ref(task_id, domain=domain, b24_user_id=b24_user_id)
    return Reply(comments.render_discussion(task_id, items, ref=ref))


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
        rows.append([keyboards.cb("s", token, title)])
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
        rows.append([keyboards.cb("p", token, label[:60])])

    row = []
    if not q.required:
        token = await issue_token("survey_skip", tenant_id=ctx.tenant_id,
                                  owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                                  payload={"session_id": session_id},
                                  ttl=timedelta(minutes=30))
        row.append(keyboards.cb("k", token, "⏭ Пропустить"))
    cancel = await issue_token("survey_cancel", tenant_id=ctx.tenant_id,
                               owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                               payload={"session_id": session_id},
                               ttl=timedelta(minutes=30))
    row.append(keyboards.cb("x", cancel, "❌ Отменить"))
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
    ref = views.task_ref(task.get("id"), domain=await _tenant_domain(ctx.tenant_id),
                         b24_user_id=b24_user_id)
    if not created:
        return Reply(texts.MSG_TASK_EXISTS.format(
            task=ref, title=esc_html(task.get("title") or "")))
    return Reply(texts.MSG_TASK_CREATED.format(
        task=ref, title=esc_html(task.get("title") or ""),
        project=esc_html(project.name),
        responsible=esc_html((task.get("responsible") or {}).get("name") or b24_user_id)))


async def _menu_tokens(ctx: ChatContext, tg_user_id: int) -> dict[str, str]:
    """Токены под кнопки меню. Общие для чата: меню закрепляют, им пользуются все."""
    actions = ("status", "overdue", "mine", "all", "new", "time")
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
    nav = [keyboards.cb("m", back, "◀️ Назад")]
    if app_url:
        nav.append(keyboards.url_button("🧩 Приложение", app_url))
    return Reply(views.render_list(tasks, title=title,
                                   domain=await _tenant_domain(ctx.tenant_id),
                                   b24_user_id=b24_user_id),
                 markup=keyboards.task_list(numbers, nav), edit=True)


async def _digest_command(ctx: ChatContext, tg_user_id: int) -> Reply:
    """`/digest` — та же сводка, что уходит по утрам, но прямо сейчас.

    Считается ЛИЧНЫМ токеном спросившего, а не сервисным, как утренняя рассылка:
    здесь есть конкретный читатель, и показывать ему больше, чем ему позволяет
    Битрикс, незачем. Числа поэтому могут честно отличаться от ночных — и это
    свойство прав, а не расхождение данных.

    Команда же и включает рассылку: экрана настроек уведомлений в системе нет, а
    настройка, до которой нельзя дотянуться, равносильна её отсутствию.
    """
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    if not ctx.has_binding:
        return Reply(texts.MSG_NO_PROJECT)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            tasks, complete = await views.fetch_open_all(
                client, [p.b24_group_id for p in ctx.projects], lane=Lane.INTERACTIVE)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24Error as exc:
        log.warning("сводка по команде не собралась: %s", exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    tz = await reminders.tenant_tz(ctx.tenant_id)
    now = datetime.now(UTC)
    digest = reminders.build_digest(
        tasks, now=now, tz=tz, complete=complete,
        awaiting=await reminders.awaiting_count(
            ctx.tenant_id, [p.id for p in ctx.projects]))
    text = reminders.render_digest(digest, day=now.astimezone(tz).date(),
                                   projects=[p.name for p in ctx.projects])

    enabled = await reminders.digest_enabled(ctx.tenant_id, ctx.chat_ref)
    text += "\n\n" + (texts.MSG_DIGEST_STATE_ON if enabled
                       else texts.MSG_DIGEST_STATE_OFF)

    markup = await reminders.digest_markup(ctx.tenant_id, ctx.chat_ref)
    rows = list(markup["inline_keyboard"])
    # Переключатель — только админу теннанта: рассылка идёт в чат клиента, и
    # включать её вправе тот же, кто решает, какие проекты в этом чате видны.
    if await access.is_tenant_admin(ctx.tenant_id, tg_user_id):
        token = await issue_token("digest", tenant_id=ctx.tenant_id,
                                  owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                                  payload={"enabled": not enabled},
                                  ttl=timedelta(hours=1))
        rows.append([keyboards.cb("dg", token,
                                  "🔕 Выключить утреннюю сводку" if enabled
                                  else "🔔 Включить утреннюю сводку")])
    return Reply(text, markup=keyboards.inline(rows))


async def _digest_toggle(ctx: ChatContext, tenant_id: int, tg_user_id: int,
                         enabled: bool) -> Reply:
    """Право проверяется второй раз, уже на нажатии: между показом кнопки и
    нажатием роль могли снять, а кнопка в чате живёт своей жизнью."""
    if not await access.is_tenant_admin(tenant_id, tg_user_id):
        return Reply(texts.MSG_NEED_TENANT_ADMIN)
    touched = await reminders.set_digest(tenant_id, ctx.chat_ref, enabled)
    if not touched:
        return Reply(texts.MSG_NO_PROJECT)
    await audit.record(tenant_id, "chat.digest.set", actor_tg_id=tg_user_id,
                       target=f"chat:{ctx.chat_ref}",
                       detail={"enabled": enabled, "bindings": touched})
    return Reply(texts.MSG_DIGEST_ON if enabled else texts.MSG_DIGEST_OFF)


async def _timesheet_months(ctx: ChatContext, tg_user_id: int) -> Reply:
    """За какой месяц показать трудозатраты. Список всегда одной длины."""
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    if not ctx.has_binding:
        return Reply(texts.MSG_NO_PROJECT)

    months = []
    for year, month in timesheet.months_back(datetime.now(UTC).date()):
        token = await issue_token(
            "menu", tenant_id=ctx.tenant_id, chat_ref=ctx.chat_ref,
            payload={"action": "time", "month": f"{year}-{month:02d}"},
            single_use=False, ttl=timedelta(days=7))
        months.append((token, timesheet.month_title(year, month)))
    back = await issue_token("menu", tenant_id=ctx.tenant_id, chat_ref=ctx.chat_ref,
                             payload={"action": "status"}, single_use=False,
                             ttl=timedelta(days=7))
    return Reply(texts.MSG_TIME_CHOOSE_MONTH,
                 markup=keyboards.month_menu(months, back), edit=True)


async def _timesheet(ctx: ChatContext, tg_user_id: int, month: str) -> Reply:
    """Свод трудозатрат за месяц по проектам чата.

    Ходим личным токеном человека: видно ровно то, что видно ему самому. Границу
    задаёт список групп чата — тот же, что и у всех остальных выборок (И-3).
    """
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)
    parsed = timesheet.parse_month(month)
    if parsed is None:
        return Reply(texts.MSG_DIALOG_EXPIRED)
    year, month_number = parsed

    group_ids = [p.b24_group_id for p in ctx.projects]
    if not group_ids:
        return Reply(texts.MSG_NO_PROJECT)

    text = await _timesheet_text(ctx.tenant_id, tg_user_id, b24_user_id, ctx.projects,
                                 year, month_number)
    back = await issue_token("menu", tenant_id=ctx.tenant_id, chat_ref=ctx.chat_ref,
                             payload={"action": "time"}, single_use=False,
                             ttl=timedelta(days=7))
    return Reply(text,
                 markup=keyboards.inline([[keyboards.cb("m", back, "◀️ Другой месяц")]]),
                 edit=True)


async def _timesheet_text(tenant_id: int, tg_user_id: int, b24_user_id: int,
                          projects: list[ProjectRef], year: int, month: int) -> str:
    """Ядро отчёта, общее для чата и для лички.

    Отличается только источник проектов: в чате это его привязки, в личке — все
    проекты теннанта, привязанные хоть к одному чату. Всё остальное — те же
    данные, те же разрезы и та же сумма.
    """
    try:
        client = await access.client_for_user(tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            snap = await timesheet.snapshot(
                client, tenant_id, [p.b24_group_id for p in projects])
    except NeedsReauth:
        return texts.MSG_NEEDS_REAUTH
    except errors.B24Error as exc:
        log.warning("трудозатраты не собрались: %s", exc)
        return texts.MSG_B24_UNAVAILABLE

    # Названия стадий — из справочника проектов: в своде их может быть несколько,
    # и колонки разных проектов встают в одном порядке с их канбаном.
    stage_titles: dict[int, str] = {}
    for project in projects:
        stage_titles.update(dict(await views.stages_of(tenant_id, project.id)))

    report = timesheet.aggregate(
        snap.entries, snap.tasks, stage_titles, year=year, month=month,
        complete=snap.complete, seen=snap.seen, total_on_portal=snap.total_on_portal,
        tag=snap.tag)
    return views.render_timesheet(report, projects)


async def _tenant_projects(tenant_id: int) -> list[ProjectRef]:
    """Проекты теннанта, привязанные хоть к одному чату, — область личных экранов."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT p.id, p.b24_group_id, p.name, c.name AS client_name
              FROM chat_bindings b
              JOIN projects p ON p.id = b.project_id AND p.status = 'active'
              JOIN clients  c ON c.id = p.client_id
             WHERE b.tenant_id = $1 AND b.status = 'active'
             ORDER BY 4, 3
            """, tenant_id)
    return [ProjectRef(r["id"], r["b24_group_id"], r["name"], r["client_name"])
            for r in rows]


async def _private_timesheet(tenant_id: int, tg_user_id: int,
                             month: str | None) -> Reply:
    """Трудозатраты в личке: тот же отчёт по всем проектам теннанта.

    Токены здесь без `chat_ref` — личных чатов в `tg_chats` нет вовсе, как и у
    кнопок подтверждения задач. Поэтому и ветка в `on_callback` своя, до загрузки
    контекста чата.
    """
    b24_user_id = await access.linked_b24_user(tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED, markup=_private_kb())
    projects = await _tenant_projects(tenant_id)
    if not projects:
        return Reply(texts.MSG_NO_PROJECT, markup=_private_kb())

    if month is None:
        months = []
        for year, number in timesheet.months_back(datetime.now(UTC).date()):
            # Свой вид токена, а не общий `menu`: тот выдаётся в группах без
            # владельца и многоразовым, и под префиксом `mt:` он открывал бы
            # отчёт по всему теннанту любому участнику любого чата.
            token = await issue_token(
                "timesheet", tenant_id=tenant_id, owner_tg_id=tg_user_id,
                payload={"month": f"{year}-{number:02d}"},
                single_use=False, ttl=timedelta(days=7))
            months.append((token, timesheet.month_title(year, number)))
        rows = [[keyboards.cb("mt", token, label)] for token, label in months]
        return Reply(texts.MSG_TIME_CHOOSE_MONTH, markup=keyboards.inline(rows))

    parsed = timesheet.parse_month(month)
    if parsed is None:
        return Reply(texts.MSG_DIALOG_EXPIRED)
    year, number = parsed
    text = await _timesheet_text(tenant_id, tg_user_id, b24_user_id, projects,
                                 year, number)
    return Reply(text)


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
    # Явные запреты нужны отдельно от разрешений: списание времени показывается
    # по отсутствию запрета (keyboards.task_card), а не по наличию ключа.
    forbidden = mapping.forbidden_actions(task)

    tokens = {}
    for act in ("complete", "start", "pause", "refresh"):
        tokens[act] = await issue_token(
            "action", tenant_id=ctx.tenant_id, owner_tg_id=tg_user_id,
            chat_ref=ctx.chat_ref, payload={"task_id": task_id, "act": act},
            single_use=(act != "refresh"), ttl=timedelta(hours=12))
    tokens["edit"] = await _edit_token(ctx, tg_user_id, task_id, "menu")
    tokens["stage"] = await _edit_token(ctx, tg_user_id, task_id, "stage_menu")
    tokens["timelog"] = await _timelog_token(ctx, tg_user_id, task_id, "menu")
    tokens["back"] = await issue_token(
        "menu", tenant_id=ctx.tenant_id, chat_ref=ctx.chat_ref,
        payload={"action": "all"}, single_use=False, ttl=timedelta(days=7))

    domain = await _tenant_domain(ctx.tenant_id)
    app_url = await miniapp.link_for_chat(ctx.tenant_id, ctx.chat_ref,
                                          thread_id=ctx.thread_id, task_id=task_id)
    stage_title = await views.resolve_stage_title(ctx.tenant_id, project,
                                                  task.get("stageId"))
    text = views.render_card(task, project, domain=domain, b24_user_id=b24_user_id,
                             stage_title=stage_title)
    return Reply(f"{note}\n\n{text}" if note else text,
                 markup=keyboards.task_card(
                     tokens, allowed=allowed, forbidden=forbidden,
                     portal_url=views.portal_task_url(domain, task_id, b24_user_id),
                     app_url=app_url),
                 edit=True)


# Действия карточки: метод портала, ожидаемый статус после него и имя в журнале.
# Имена аудита общие с мини-аппом (`api/miniapp.py`): одно и то же действие обязано
# называться одинаково, откуда бы его ни сделали, иначе журнал бесполезен для разбора.
# Расхождение ловит тест-страж `tests/test_audit_names.py`.
ACTION_METHODS = {"complete": "tasks.task.complete", "start": "tasks.task.start",
                  "pause": "tasks.task.pause", "defer": "tasks.task.defer",
                  "renew": "tasks.task.renew"}
ACTION_STATUS = {"complete": 5, "start": 3, "pause": 2, "defer": 6, "renew": 2}
ACTION_AUDIT = {"complete": "task.complete", "defer": "task.defer",
                "start": "task.status.change", "pause": "task.status.change",
                "renew": "task.status.change"}


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


# -------------------------------------------------------------- трудозатраты
async def _timelog_pick(ctx: ChatContext, tg_user_id: int,
                        b24_user_id: int) -> Reply:
    """Список задач чата, под каждой — своя кнопка списания времени.

    Свои задачи вперёд: списывают время в первую очередь себе. Но если на
    человека в этих проектах ничего не назначено, список не схлопывается в
    пустоту — показываются все открытые, и об этом сказано текстом. Пустой ответ
    на «списать время» читался бы как поломка, а не как «вам ничего не поручено».
    """
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    group_ids = [p.b24_group_id for p in ctx.projects]
    if not group_ids:
        # «Задач не видно» и «чат не привязан» — разные вещи, и второе человек
        # может починить сам. Область выборки задаёт только `GROUP_ID`, поэтому
        # пустой список и не превращается в «все задачи портала» (views.fetch_open).
        return Reply(texts.MSG_NO_PROJECT)
    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            tasks = await views.fetch_open(client, group_ids)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24Error as exc:
        log.warning("не удалось получить задачи для списания: %s", exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    async def token(task_id: int) -> str:
        return await _timelog_token(ctx, tg_user_id, task_id, "menu")

    return await _timelog_pick_reply(ctx.tenant_id, b24_user_id, tasks, issue=token)


async def _timelog_pick_reply(tenant_id: int, b24_user_id: int,
                              tasks: list[dict[str, Any]], *,
                              issue: Callable[[int], Awaitable[str]],
                              private: bool = False) -> Reply:
    """Общее тело обоих списков — чата и лички.

    Различий у них ровно два: откуда взялись задачи и под каким префиксом уедет
    кнопка. Всё остальное — какие задачи считать своими, что делать, когда своих
    нет, и как признаться в усечении — обязано быть одним: разъедься эти две
    реализации, человек получал бы в личке и в чате разные списки на один вопрос.
    """
    mine = [t for t in tasks if str(t.get("responsibleId")) == str(b24_user_id)]
    note = "" if mine else texts.MSG_TIMELOG_PICK_NONE_MINE
    chosen = mine or tasks
    if not chosen:
        return Reply(texts.MSG_TIMELOG_PICK_EMPTY)

    domain = await _tenant_domain(tenant_id)
    title = "⏱ Мои задачи" if mine else "⏱ Открытые задачи"
    text = "\n\n".join(x for x in (
        texts.MSG_TIMELOG_PICK, note,
        views.render_list(chosen, title=title, domain=domain,
                          b24_user_id=b24_user_id)) if x)

    # Порядок и предел — те же, что у текста списка: кнопка обязана стоять под
    # той задачей, которую человек читает, а не под одиннадцатой из другой сортировки.
    shown = views.flatten_for_buttons(chosen)
    if len(chosen) > len(shown):
        # Молчаливое усечение выглядит как баг продукта: сколько показано и
        # сколько всего — обязательная часть ответа.
        text += texts.MSG_TIMELOG_PICK_MORE.format(shown=len(shown),
                                                   total=len(chosen))
    items = [(await issue(int(t["id"])), f"⏱ #{int(t['id'])}") for t in shown]
    # `edit` сработает только там, где есть что переписывать, — при возврате
    # кнопкой «◀️ К списку». Команда и кнопка постоянной клавиатуры шлют обычное
    # сообщение, и ответ на них уедет новым.
    return Reply(text, markup=keyboards.timelog_pick(items, private=private),
                 edit=private)


async def _timelog_token(ctx: ChatContext, tg_user_id: int, task_id: int, act: str,
                         seconds: int | None = None) -> str:
    """Токен кнопки списания.

    Экран можно открывать сколько угодно, а само списание одноразовое: два клика
    по «1 ч» подряд создали бы две записи по часу, и отличить это от честного
    «ещё час» уже не смог бы никто — ни мы, ни человек.
    """
    payload: dict[str, Any] = {"task_id": task_id, "act": act}
    if seconds is not None:
        payload["seconds"] = seconds
    return await issue_token("timelog", tenant_id=ctx.tenant_id,
                             owner_tg_id=tg_user_id, chat_ref=ctx.chat_ref,
                             payload=payload, single_use=(act == "add"),
                             ttl=timedelta(hours=12))


async def _timelog(ctx: ChatContext, tg_user_id: int, payload: dict[str, Any]) -> Reply:
    """Экран списаний задачи и быстрое списание кнопкой."""
    task_id = int(payload["task_id"])
    act = str(payload.get("act") or "menu")
    if act == "back":
        return await _open_card(ctx, tg_user_id, task_id)
    if act == "ask":
        return _timelog_ask(task_id)
    if ctx.tenant_id is None:
        return Reply(texts.MSG_NOT_CLAIMED)
    b24_user_id = await access.linked_b24_user(ctx.tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)

    seconds = mapping.as_int(payload.get("seconds")) if act == "add" else None
    if act == "add" and seconds is None:
        return Reply(texts.MSG_DIALOG_EXPIRED)

    try:
        client = await access.client_for_user(ctx.tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            task = await task_service.read(client, task_id)
            # И-3: право открыть задачу даёт не токен кнопки, а принадлежность
            # задачи проекту ЭТОГО чата. Проверяется на каждом нажатии заново.
            project = await authorize_task_for_chat(
                ctx.tenant_id, ctx.chat_ref, task_id,
                group_id_hint=mapping.as_int(task.get("groupId")))
            if project is None:
                return Reply(texts.MSG_TASK_NOT_FOUND)

            if seconds is not None:
                await timelog.add(client, task_id, seconds)
                await _audit_timelog(ctx.tenant_id, b24_user_id, tg_user_id,
                                     task_id, project.id, seconds, source="bot")
                task = await task_service.read(client, task_id)
                note = texts.MSG_TIMELOG_DONE.format(
                    task_id=task_id, duration=timelog.format_duration(seconds))
                return await _render_card(ctx, tg_user_id, b24_user_id, task,
                                          project, note)

            return await _timelog_screen(ctx, tg_user_id, client, task, project)
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24AccessDenied as exc:
        return Reply(texts.MSG_NO_RIGHTS_B24.format(reason=esc_html(exc.description)))
    except errors.B24Error as exc:
        log.warning("списание времени в задачу %s не удалось: %s", task_id, exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)


async def _timelog_screen(ctx: ChatContext, tg_user_id: int, client: Any,
                          task: dict[str, Any], project: ProjectRef) -> Reply:
    """Кто сколько списал плюс кнопки быстрых длительностей."""
    task_id = mapping.as_int(task.get("id")) or 0
    total = mapping.as_int(task.get("timeSpentInLogs")) or 0
    entries = await timelog.for_task(client, task_id, total)
    names = await task_service.user_names(client, [e.user_id for e in entries.entries])

    items = [(await _timelog_token(ctx, tg_user_id, task_id, "add", value),
              timelog.preset_label(value))
             for value in timelog.PRESETS]
    back = await _timelog_token(ctx, tg_user_id, task_id, "back")
    ask = await _timelog_token(ctx, tg_user_id, task_id, "ask")
    app_url = await miniapp.link_for_chat(ctx.tenant_id or 0, ctx.chat_ref,
                                          thread_id=ctx.thread_id, task_id=task_id)

    text = (views.render_timelog(task_id, entries, names,
                                 task_title=str(task.get("title") or ""))
            + "\n\n" + texts.MSG_TIMELOG_MENU.format(task_id=task_id)
            + "\n" + texts.MSG_TIMELOG_HINT.format(task_id=task_id))
    return Reply(text, markup=keyboards.timelog_menu(items, back, ask, app_url),
                 edit=True)


# ------------------------------------------- свободный ввод длительности
# Своего состояния у этого ввода нет и не заводится: вопрос уже написан в чате,
# а реплай приносит его текст обратно вместе с номером задачи. Опросник хранит
# `last_message_id`, потому что у него есть сессия на одного человека; здесь
# сессии нет — списать время в ответ на приглашение вправе любой участник, и
# каждый своим токеном.
_ASK_TASK = re.compile(r"Списать время в задачу\s*#(\d+)")


def timelog_ask_task_id(text: str) -> int | None:
    """Номер задачи из нашего приглашения «✏️ Другое», если это оно.

    Разметку снимаем: из Telegram текст приходит уже без тегов, а из
    `texts.MSG_TIMELOG_ASK` — с ними, и страж обязан ходить тем же путём,
    что живой реплай. Разъедься фраза и это выражение — кнопка «Другое»
    молча перестала бы принимать ответы.
    """
    plain = re.sub(r"<[^>]+>", "", str(text or ""))
    found = _ASK_TASK.search(plain)
    return int(found.group(1)) if found else None


def _timelog_ask(task_id: int) -> Reply:
    """Приглашение к вводу — НОВЫМ сообщением и с полем ответа.

    Редактированием тут не обойтись при всём желании: `force_reply` живёт
    только в `sendMessage`, а `editMessageText` принимает исключительно
    инлайн-клавиатуру. Экран списаний при этом остаётся в чате нетронутым —
    и это к лучшему: человек видит, к чему относится вопрос.
    """
    return Reply(texts.MSG_TIMELOG_ASK.format(task_id=task_id),
                 markup=keyboards.force_reply(texts.MSG_TIMELOG_PLACEHOLDER))


def _asked_timelog_task(reply_to: dict[str, Any], bot: dict[str, Any]) -> int | None:
    """Это ответ на НАШЕ приглашение? Иначе None — сообщение не наше дело.

    Автора проверяем первым: тот же текст мог напечатать и человек, а реплай
    на чужое сообщение — обычная реплика в переписке, съедать её нельзя.
    """
    author = reply_to.get("from") or {}
    if not author.get("is_bot") or int(author.get("id") or 0) != int(bot["bot_id"]):
        return None
    return timelog_ask_task_id(_text_of(reply_to))


def _timelog_retry(task_id: int, reason: str) -> Reply:
    """Не разобрали — переспрашиваем тем же полем ответа.

    Приглашение повторяется целиком, вместе с номером задачи: без него следующий
    реплай прилетел бы на сообщение, в котором номера нет, и опознать его было бы
    нечем — то есть за опечатку человек платил бы возвратом к карточке.
    """
    return Reply(texts.MSG_TIMELOG_BAD_DURATION.format(reason=esc_html(reason))
                 + "\n\n" + texts.MSG_TIMELOG_ASK.format(task_id=task_id),
                 markup=keyboards.force_reply(texts.MSG_TIMELOG_PLACEHOLDER))


async def _timelog_answer(ctx: ChatContext, msg: dict[str, Any],
                          reply_to: dict[str, Any], tg_user_id: int,
                          bot: dict[str, Any]) -> Reply | None:
    """Ответ длительностью в группе. None означает «это не про списание»."""
    task_id = _asked_timelog_task(reply_to, bot)
    if task_id is None:
        return None
    try:
        seconds = timelog.parse_duration(_text_of(msg))
    except timelog.BadDuration as exc:
        return _timelog_retry(task_id, str(exc))
    # Дальше — общий путь с кнопкой быстрой длительности: те же проверки прав,
    # та же запись, тот же аудит. Второй дороги к `timelog.add` не заводится.
    return await _timelog(ctx, tg_user_id,
                          {"task_id": task_id, "act": "add", "seconds": seconds})


async def _dm_timelog_answer(tg_user_id: int, task_id: int, text: str) -> Reply:
    """То же в личке: контекста чата нет, проверка идёт по теннанту."""
    try:
        seconds = timelog.parse_duration(text)
    except timelog.BadDuration as exc:
        return _timelog_retry(task_id, str(exc))
    tenant_id = await access.tenant_of_user(tg_user_id)
    if tenant_id is None:
        return Reply(texts.MSG_NOT_LINKED, markup=_private_kb())
    return await _dm_timelog(tenant_id, tg_user_id,
                             {"task_id": task_id, "act": "add", "seconds": seconds})


async def _audit_timelog(tenant_id: int, b24_user_id: int, tg_user_id: int | None,
                         task_id: int, project_id: int, seconds: int,
                         *, source: str) -> None:
    """Имя берётся из `timelog.AUDIT_ACTION`: одно действие — одно имя."""
    await audit.record(tenant_id, timelog.AUDIT_ACTION, actor_id=b24_user_id,
                       actor_tg_id=tg_user_id, target=f"task:{task_id}",
                       project_id=project_id,
                       detail={"seconds": seconds, "source": source})


# ------------------------------------------------------------- редактирование
EDIT_AUDIT = {"responsible_id": "task.responsible.change",
              "deadline": "task.deadline.change",
              "priority": "task.priority.change"}
PRIORITY_LABELS = {0: "низкий", 1: "средний", 2: "высокий"}
DEADLINE_LABELS = {"today": "сегодня", "tomorrow": "завтра", "in3": "через 3 дня",
                   "week": "через неделю", "clear": "снят"}
ASSIGNEE_PAGE = 12  # больше кнопок в один экран телефона всё равно не влезает
STAGE_PAGE = 12     # столько же: колонок канбана обычно 3–5, но предел нужен и тут


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
            if act == "stage_menu":
                # Список берём С ПОРТАЛА, а не из своего справочника: колонку могли
                # завести пять минут назад, и показать неполный список — значит
                # оставить человека гадать, по какому принципу её тут нет.
                stages = await task_service.stages_of_group(client,
                                                            project.b24_group_id)
                return await _stage_menu(ctx, tg_user_id, task_id, stages, task)

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

    what = _edit_summary(act, payload, patch)
    if act == "set_stage":
        what += " — " + await views.resolve_stage_title(ctx.tenant_id, project,
                                                        fresh.get("stageId"))
    note = texts.MSG_EDIT_DONE.format(what=esc_html(what))
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
    if act == "set_stage":
        return task_service.validate_patch({"stage_id": value})
    return None


def _edit_summary(act: str, payload: dict[str, Any], patch: dict[str, Any]) -> str:
    if act == "set_deadline":
        return f"срок — {DEADLINE_LABELS.get(str(payload.get('value')), 'изменён')}"
    if act == "set_priority":
        return f"приоритет — {PRIORITY_LABELS.get(int(patch['priority']), '?')}"
    if act == "set_stage":
        # Название стадии подставляет вызывающий: здесь его взять неоткуда,
        # а «стадия — 337» человеку ничего не говорит.
        return "стадия"
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


async def _stage_menu(ctx: ChatContext, tg_user_id: int, task_id: int,
                      stages: list[dict[str, Any]], task: dict[str, Any]) -> Reply:
    """Колонки канбана проекта кнопками.

    Стадия и статус независимы (docs/00-portal-facts.md §3.2): «Сделаны» в канбане
    не завершает задачу, поэтому меню отдельное, а не спрятано под «Завершить».
    """
    if not stages:
        return Reply(texts.MSG_EDIT_NO_STAGES)

    shown = stages[:STAGE_PAGE]
    current = mapping.as_int(task.get("stageId")) or 0
    items = []
    for st in shown:
        token = await _edit_token(ctx, tg_user_id, task_id, "set_stage", st["id"])
        # Текущую колонку помечаем: иначе непонятно, откуда двигаем.
        mark = "✅ " if int(st["id"]) == current else ""
        items.append((token, f"{mark}{st['title']}"))
    # Назад — к карточке, откуда кнопка и нажата: меню правки к стадии больше
    # не ведёт, и возвращать туда значило бы уводить человека в сторону.
    back = await _edit_token(ctx, tg_user_id, task_id, "back")

    text = texts.MSG_EDIT_STAGE.format(task_id=task_id)
    if len(stages) > len(shown):
        text += texts.MSG_EDIT_TRUNCATED.format(shown=len(shown), total=len(stages))
    app_url = await miniapp.link_for_chat(ctx.tenant_id or 0, ctx.chat_ref,
                                          thread_id=ctx.thread_id, task_id=task_id)
    return Reply(text, markup=keyboards.stage_menu(items, back, app_url), edit=True)


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
                   text: str = "", msg: dict[str, Any] | None = None) -> Reply | None:
    if cmd is None:
        # Ответ на приглашение «✏️ Другое». Реплаем, как и в группе: собеседник
        # тут один, но правило ввода общее, а второе правило для лички означало
        # бы, что одно и то же сообщение в двух местах понимается по-разному.
        reply_to = (msg or {}).get("reply_to_message") or {}
        task_id = _asked_timelog_task(reply_to, bot) if reply_to else None
        if task_id is not None:
            return await _dm_timelog_answer(tg_user_id, task_id, text)
        # Постоянная клавиатура шлёт обычный ТЕКСТ, а не callback. Без разбора
        # подписей любое нажатие выглядело как «бот не реагирует».
        action = keyboards.PRIVATE_LABELS.get(text.strip())
        if action:
            return await _private_action(action, tg_user_id)
        return Reply(_help_text(private=True), markup=_private_kb())
    name, arg = cmd

    if name == "start" and arg == linking.START_ARG:
        # Пришёл по кнопке из группы: там личную ссылку показывать нельзя.
        await _ensure_menu_button(bot, tg_user_id)
        return await _link_offer(bot, tg_user_id, user)
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
        return await _link_offer(bot, tg_user_id, user)
    # Те же действия, что на постоянной клавиатуре: человек, привыкший к слешам,
    # не должен искать кнопку, а пришедший из меню Telegram — знать про кнопки.
    # Имена действий — те же, что у подписей кнопок (`keyboards.PRIVATE_LABELS`):
    # два названия одного и того же расходятся молча, а ветка разбора у них общая.
    slash_actions = {"status": "mytasks", "list": "mytasks",
                     "overdue": "overdue", "mychats": "mychats",
                     "pending": "pending", "time": "timelog"}
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

    if action == "timesheet":
        return await _private_timesheet(tenant_id, tg_user_id, None)

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

    if action == "timelog":
        return await _dm_timelog_pick(tenant_id, tg_user_id, b24_user_id, tasks)

    if action == "overdue":
        tasks = [t for t in tasks if views.is_overdue(t)]
        title = "🔥 Просроченные"
    elif action == "mytasks":
        tasks = [t for t in tasks if str(t.get("responsibleId")) == str(b24_user_id)]
        title = "📊 Мои задачи"
    else:
        # Досюда доезжает только действие, для которого забыли ветку. Раньше
        # такое молча показывало «Мои задачи» — то есть кнопка отвечала чужим
        # экраном, и отличить это от задуманного было нечем. Ветку держит пустой
        # страж `tests/test_commands.py::test_every_private_action_is_handled`.
        log.warning("действие лички без ветки: %s", action)
        return Reply(_help_text(private=True), markup=_private_kb())
    return Reply(views.render_list(tasks, title=title,
                                   domain=await _tenant_domain(tenant_id),
                                   b24_user_id=b24_user_id),
                 markup=_private_kb())


# ------------------------------------------------- списание времени в личке
# Личных чатов нет в `tg_chats` (dispatch.py), поэтому ChatContext здесь взять
# неоткуда: у экрана свой префикс кнопок (`tm`), свой вид токена и своя проверка
# доступа — по теннанту, а не по привязке чата. Ровно так же устроены остальные
# личные экраны: подтверждение задач и отчёт по трудозатратам.
async def _dm_timelog_token(tenant_id: int, tg_user_id: int, task_id: int, act: str,
                            seconds: int | None = None) -> str:
    """Токен кнопки списания в личке. Экран многоразовый, само списание — нет."""
    payload: dict[str, Any] = {"task_id": task_id, "act": act}
    if seconds is not None:
        payload["seconds"] = seconds
    return await issue_token("timelog_dm", tenant_id=tenant_id,
                             owner_tg_id=tg_user_id, payload=payload,
                             single_use=(act == "add"), ttl=timedelta(hours=12))


async def _dm_timelog_pick(tenant_id: int, tg_user_id: int, b24_user_id: int,
                           tasks: list[dict[str, Any]]) -> Reply:
    """Выбор задачи кнопкой: то же, что `/time` в чате, только по всем проектам."""
    async def token(task_id: int) -> str:
        return await _dm_timelog_token(tenant_id, tg_user_id, task_id, "menu")

    return await _timelog_pick_reply(tenant_id, b24_user_id, tasks,
                                     issue=token, private=True)


async def _dm_timelog(tenant_id: int, tg_user_id: int,
                      payload: dict[str, Any]) -> Reply:
    """Экран списаний задачи в личке и быстрое списание кнопкой."""
    act = str(payload.get("act") or "menu")
    if act == "list":
        return await _private_action("timelog", tg_user_id)
    if act == "ask":
        return _timelog_ask(int(payload["task_id"]))

    b24_user_id = await access.linked_b24_user(tenant_id, tg_user_id)
    if b24_user_id is None:
        return Reply(texts.MSG_NOT_LINKED)
    task_id = int(payload["task_id"])
    seconds = mapping.as_int(payload.get("seconds")) if act == "add" else None
    if act == "add" and seconds is None:
        return Reply(texts.MSG_DIALOG_EXPIRED)

    try:
        client = await access.client_for_user(tenant_id, b24_user_id,
                                              actor_tg_user_id=tg_user_id)
        async with client:
            task = await task_service.read(client, task_id)
            # Граница И-3 в личке: задача обязана принадлежать проекту этого
            # теннанта, привязанному хоть к одному живому чату. Проверяется на
            # каждом нажатии заново, а не один раз при выдаче кнопки.
            project = await authorize_task_for_tenant(
                tenant_id, task_id,
                group_id_hint=mapping.as_int(task.get("groupId")))
            if project is None:
                return Reply(texts.MSG_TASK_NOT_FOUND)

            note = ""
            if seconds is not None:
                await timelog.add(client, task_id, seconds)
                await _audit_timelog(tenant_id, b24_user_id, tg_user_id, task_id,
                                     project.id, seconds, source="bot")
                task = await task_service.read(client, task_id)
                note = texts.MSG_TIMELOG_DONE.format(
                    task_id=task_id, duration=timelog.format_duration(seconds))

            total = mapping.as_int(task.get("timeSpentInLogs")) or 0
            entries = await timelog.for_task(client, task_id, total)
            names = await task_service.user_names(
                client, [e.user_id for e in entries.entries])
    except NeedsReauth:
        return Reply(texts.MSG_NEEDS_REAUTH)
    except errors.B24AccessDenied as exc:
        return Reply(texts.MSG_NO_RIGHTS_B24.format(reason=esc_html(exc.description)))
    except errors.B24Error as exc:
        log.warning("списание времени в задачу %s не удалось: %s", task_id, exc)
        return Reply(texts.MSG_B24_UNAVAILABLE)

    items = [(await _dm_timelog_token(tenant_id, tg_user_id, task_id, "add", value),
              timelog.preset_label(value))
             for value in timelog.PRESETS]
    back = await _dm_timelog_token(tenant_id, tg_user_id, task_id, "list")
    ask = await _dm_timelog_token(tenant_id, tg_user_id, task_id, "ask")
    text = (views.render_timelog(task_id, entries, names,
                                 task_title=str(task.get("title") or ""))
            + "\n\n" + texts.MSG_TIMELOG_MENU.format(task_id=task_id)
            + "\n" + texts.MSG_TIMELOG_HINT.format(task_id=task_id))
    if note:
        text = f"{note}\n\n{text}"
    return Reply(text, markup=keyboards.timelog_menu(items, back, ask, private=True),
                 edit=True)


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

    # Домен и свой номер в Битриксе спрашиваем один раз на весь список, а не на
    # каждую строку: строк тут до пятнадцати.
    domain = await _tenant_domain(tenant_id)
    b24_user_id = await access.linked_b24_user(tenant_id, tg_user_id)

    lines = ["<b>🙋 Ожидают вашего подтверждения</b>", ""]
    buttons: list[list[dict[str, str]]] = []
    for item in items:
        ref = views.task_ref(item.b24_task_id, domain=domain, b24_user_id=b24_user_id)
        lines.append(f"{ref} · {esc_html(item.task_title)}")
        lines.append(f"    {esc_html(item.client_name)} · {esc_html(item.project_name)}")
        confirm = await issue_token(
            "task_approval", tenant_id=tenant_id, owner_tg_id=tg_user_id,
            payload={"approval_id": item.id, "decision": "confirm"}, ttl=timedelta(days=30))
        reject = await issue_token(
            "task_approval", tenant_id=tenant_id, owner_tg_id=tg_user_id,
            payload={"approval_id": item.id, "decision": "reject"}, ttl=timedelta(days=30))
        buttons.append([
            keyboards.cb("av", confirm, f"✅ #{item.b24_task_id}"),
            keyboards.cb("av", reject, f"❌ #{item.b24_task_id}"),
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
    ref = views.task_ref(result.task_id, domain=await _tenant_domain(tenant_id),
                         b24_user_id=await access.linked_b24_user(tenant_id, tg_user_id))

    if result.outcome == "confirmed":
        text = texts.MSG_APPROVAL_CONFIRMED.format(
            task=ref, title=esc_html(result.title),
            stage=esc_html(result.stage_title))
    elif result.outcome == "rejected":
        text = texts.MSG_APPROVAL_REJECTED.format(
            task=ref, title=esc_html(result.title),
            stage=esc_html(result.stage_title))
    elif result.outcome == "already_done":
        text = texts.MSG_APPROVAL_ALREADY_DONE.format(
            task=ref, title=esc_html(result.title))
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
    """Завершение привязки: человек открыл приложение в Б24 и перешёл по deep link.

    Дверь «портал → Telegram». Запись при этом идёт тем же `link_accounts`, что и
    у двери «Telegram → портал»: одинаковый результат обязан оставлять в базе
    одинаковое состояние, иначе расхождение видно только по жалобе.
    """
    row = await consume_token(token, None)
    if row is None or row["kind"] != "link":
        return Reply(texts.MSG_LINK_BAD_TOKEN)

    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    tenant_id, b24_user_id = int(row["tenant_id"]), int(payload["b24_user_id"])

    replaced = await linking.link_accounts(
        tenant_id, b24_user_id, tg_user_id,
        tg_username=user.get("username"), display_name=_author(user))
    await audit.record(tenant_id, "user.map", actor_kind="user", actor_id=b24_user_id,
                       actor_tg_id=tg_user_id, target=f"b24_user:{b24_user_id}",
                       detail={"источник": "приложение в Битрикс24",
                               **({"отобрана у tg": replaced} if replaced else {})})
    if replaced:
        await dm.send(tenant_id, replaced,
                      texts.MSG_LINK_TAKEN_OVER.format(b24_user_id=b24_user_id))

    log.info("привязка завершена: теннант %s, Б24 %s, TG %s",
             tenant_id, b24_user_id, tg_user_id)
    return Reply(texts.MSG_LINK_DONE)


async def _link_offer(bot: dict[str, Any], tg_user_id: int,
                      user: dict[str, Any]) -> Reply:
    """`/link` в личке: личная ссылка на экран согласия портала.

    Теннант берётся у бота, а не у чата: бот у теннанта ровно один
    (`tg_bots.tenant_id UNIQUE`), и в личке другого источника нет вовсе.
    """
    raw_tenant = bot.get("tenant_id")
    tenant_id = int(raw_tenant) if raw_tenant is not None else 0
    started = await linking.begin(
        tenant_id, tg_user_id, tg_username=user.get("username"),
        display_name=_author(user)) if tenant_id else None
    if started is None:
        return Reply(texts.MSG_LINK_UNAVAILABLE, markup=_private_kb())

    linked = await access.linked_b24_user(tenant_id, tg_user_id)
    text = (texts.MSG_LINK_RELINK.format(b24_user_id=linked) if linked
            else texts.MSG_LINK_OFFER.format(portal=esc_html(started.portal_domain)))
    # Инлайн-кнопка вместо постоянной клавиатуры: их нельзя послать одним
    # сообщением, а ссылка тут и есть всё сообщение.
    # Подпись называет результат, а не шаг: человек нажимает, чтобы привязать
    # аккаунт, а вход в портал — то, что случится по дороге.
    return Reply(text, buttons=[[keyboards.url_button("🔗 Привязать",
                                                      started.url)]])


def _link_to_private(bot: dict[str, Any]) -> Reply:
    """`/link` в ГРУППЕ: зовём в личку, ссылку в общий чат не кладём.

    Ссылка на экран согласия — это разрешение записать, что вошедший и есть
    владелец ЭТОГО телеграм-аккаунта. Показанная всему чату, она превращается в
    приглашение отдать свой доступ к Битриксу первому, кто нажмёт.
    """
    url = f"https://t.me/{bot['username']}?start={linking.START_ARG}"
    return Reply(texts.MSG_LINK_IN_PRIVATE,
                 buttons=[[keyboards.url_button("🔗 Привязать в личке", url)]])


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
        return _link_to_private(bot)

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
    if name == "digest":
        return await _digest_command(ctx, tg_user_id)
    if name == "comment":
        return await _comment_command(ctx, msg, arg, tg_user_id)
    if name == "time":
        return await _time_command(ctx, msg, arg, tg_user_id)
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
            buttons.append([keyboards.cb("b", token,
                                        f"{r['client_name']} · {r['name']}")])
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
        buttons.append([keyboards.cb("b", token, title[:60])])

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
            buttons.append([keyboards.cb("tp", token,
                                        f"{p.client_name} · {p.name}")])
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

    ref = views.task_ref(task.get("id"), domain=await _tenant_domain(ctx.tenant_id),
                         b24_user_id=b24_user_id)
    if not created:
        return Reply(texts.MSG_TASK_EXISTS.format(
            task=ref, title=esc_html(task.get("title") or "")))

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
        task=ref, title=esc_html(task.get("title") or ""),
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
def _keep_source(payload: dict[str, Any], reply: Reply) -> Reply:
    """Не затирать сообщение, под которым нажали кнопку, если это уведомление.

    Списки и карточки живут в одном сообщении и редактируются на месте — так чат
    не превращается в ленту. С уведомлением так нельзя: оно пришло само, это
    запись о событии, и заменить её карточкой значит стереть из истории чата то,
    о чём было сообщение. Признак `notify` ставится при постановке уведомления
    в очередь (`domain/events.py`).
    """
    if payload.get("notify"):
        reply.edit = False
    return reply


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

    # Префикс и вид токена связаны взаимно однозначно (bot/callbacks.py). Расхождение
    # означает либо наш разлад эмиттера с роутером, либо чужой токен под подставленным
    # префиксом: токены меню выдаются без владельца и многоразовыми, то есть доступны
    # любому участнику чата, а ветка под другим префиксом ждёт совсем другой payload.
    # Отказ тот же, что у истёкшего токена: разный текст работал бы оракулом.
    if not callbacks.accepts(ns, str(row["kind"])):
        log.warning("токен вида %r приехал под префиксом %r", row["kind"], ns)
        return Reply(texts.MSG_DIALOG_EXPIRED)

    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]

    if ns == "mt":
        # Личный отчёт по трудозатратам: как и подтверждение задач, живёт в личке,
        # где ChatContext взять неоткуда — личных чатов в tg_chats нет.
        if row["tenant_id"] is None:
            return Reply(texts.MSG_DIALOG_EXPIRED)
        return await _private_timesheet(int(row["tenant_id"]), tg_user_id,
                                        payload.get("month"))

    if ns == "tm":
        # Списание времени в личке. Как и отчёт выше, живёт там, где ChatContext
        # взять неоткуда, поэтому разбирается до его загрузки.
        if row["tenant_id"] is None:
            return Reply(texts.MSG_DIALOG_EXPIRED)
        return await _dm_timelog(int(row["tenant_id"]), tg_user_id, payload)

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

    if ns == "dg":
        return await _digest_toggle(ctx, tenant_id, tg_user_id,
                                    bool(payload.get("enabled")))
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
        if action == "time":
            month = payload.get("month")
            if month is None:
                return await _timesheet_months(ctx, tg_user_id)
            return await _timesheet(ctx, tg_user_id, str(month))
        return await _open_summary(ctx, tg_user_id, action)
    if ns == "t":
        return _keep_source(payload,
                            await _open_card(ctx, tg_user_id, int(payload["task_id"])))
    if ns == "d":
        return _keep_source(payload,
                            await _discussion_for(ctx, int(payload["task_id"]),
                                                  tg_user_id))
    if ns == "a":
        return _keep_source(payload,
                            await _task_action(ctx, tg_user_id, int(payload["task_id"]),
                                               str(payload.get("act") or "refresh")))
    if ns == "e":
        return _keep_source(payload, await _edit(ctx, tg_user_id, payload))
    if ns == "tl":
        return await _timelog(ctx, tg_user_id, payload)
    if ns == "b":
        return await _apply_bind(ctx, tenant_id, payload, tg_user_id)
    if ns == "tp":
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
