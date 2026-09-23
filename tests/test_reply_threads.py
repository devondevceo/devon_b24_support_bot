"""Реплай в обычной супергруппе и в теме форума.

`message_thread_id` в Telegram означает две разные вещи, и различает их только
`is_topic_message`:

* в теме форума это тема — отвечать надо в неё;
* в обычной супергруппе его получает ЛЮБОЙ реплай, и там это всего лишь id первого
  сообщения в цепочке ответов. Отправка с ним не работает (python-telegram-bot:
  «It does not work if the thread is a chain of replies to a message in a normal
  group», `Message._parse_message_thread_id`; aiogram передаёт его только при
  `is_topic_message`).

Бот брал `message_thread_id` как есть. Отсюда жалоба 23.09.2026 «бот не создал
задачу через реплай на /task»: задача создавалась, а ответ о ней уходил с номером
цепочки ответов вместо темы и терялся — в логе оставалось предупреждение, в чате
тишина. В чате с несколькими проектами терялся вопрос «в каком проекте?», и задача
не создавалась вовсе. Опросник по той же причине не узнавал ответ реплаем на свой
же вопрос: сессия ключевалась «темой», которой у обычной группы нет.

Вторая половина — тема форума: там Telegram делает реплаем КАЖДОЕ сообщение, и без
явного ответа `reply_to_message` указывает на служебное сообщение о создании темы.
`/task текст` принимал его за реплай и отвечал «не вижу текста для задачи».
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from b24bot.bot import dispatch, handlers, survey, texts
from b24bot.domain import context, lifecycle
from b24bot.tg import api as tg

SRC = Path(__file__).resolve().parents[1] / "src" / "b24bot"

BOT_ID = 900
BOT = {"id": 1, "tenant_id": 1, "bot_id": BOT_ID, "username": "devon_sd_bot",
       "token": "t"}
BOT_USER = {"id": BOT_ID, "is_bot": True, "first_name": "Devon SD",
            "username": "devon_sd_bot"}
ALICE = {"id": 1, "first_name": "Сергей", "username": "skr"}
BOB = {"id": 77, "first_name": "Роман", "username": "rd"}

PLAIN = {"id": -1001234567890, "type": "supergroup", "title": "Поддержка"}
FORUM = {"id": -1009876543210, "type": "supergroup", "title": "Поддержка",
         "is_forum": True}
TOPIC = 7

COMMAND = [{"type": "bot_command", "offset": 0, "length": 5}]

# Служебное сообщение о создании темы: на него Telegram «отвечает» каждым
# сообщением темы, в котором человек ни на что не отвечал.
TOPIC_CREATED = {"message_id": TOPIC, "message_thread_id": TOPIC,
                 "is_topic_message": True, "from": ALICE, "chat": FORUM,
                 "forum_topic_created": {"name": "Линия Жизни",
                                         "icon_color": 7322096}}

REQUEST = {"message_id": 119, "from": ALICE, "chat": PLAIN,
           "text": "У клиента не открывается форма записи"}

# `/task` реплаем в обычной супергруппе — ровно то, что прислал бы Telegram:
# у реплая есть `message_thread_id` (начало цепочки), а `is_topic_message` нет.
PLAIN_TASK_REPLY = {"message_id": 120, "message_thread_id": 119, "from": BOB,
                    "chat": PLAIN, "text": "/task", "entities": COMMAND,
                    "reply_to_message": REQUEST}


def _ctx(chat: dict[str, Any], thread_id: int | None,
         projects: int = 1) -> context.ChatContext:
    return context.ChatContext(
        chat_ref=5, chat_id=int(chat["id"]), title=str(chat["title"]),
        status="active", tenant_id=1, is_forum=bool(chat.get("is_forum")),
        thread_id=thread_id,
        projects=[context.ProjectRef(11 + i, 101 + i, f"Проект {i}", "Линия Жизни")
                  for i in range(projects)])


# ----------------------------------------------------------------- разбор
def test_reply_chain_in_a_plain_supergroup_is_not_a_topic() -> None:
    assert handlers.topic_of(PLAIN_TASK_REPLY) is None


def test_forum_topic_is_a_topic() -> None:
    msg = {"message_id": 30, "message_thread_id": TOPIC, "is_topic_message": True,
           "chat": FORUM, "text": "привет"}
    assert handlers.topic_of(msg) == TOPIC


def test_message_without_a_thread_has_no_topic() -> None:
    assert handlers.topic_of({"message_id": 1, "chat": PLAIN, "text": "/task"}) is None


def test_plain_reply_is_a_reply() -> None:
    assert handlers.reply_of(PLAIN_TASK_REPLY) == REQUEST


def test_implicit_reply_to_the_topic_creation_is_not_a_reply() -> None:
    msg = {"message_id": 30, "message_thread_id": TOPIC, "is_topic_message": True,
           "chat": FORUM, "text": "/task", "reply_to_message": TOPIC_CREATED}
    assert handlers.reply_of(msg) is None


def test_explicit_reply_inside_a_topic_is_a_reply() -> None:
    target = {"message_id": 29, "message_thread_id": TOPIC, "is_topic_message": True,
              "from": ALICE, "chat": FORUM, "text": "Не грузится личный кабинет"}
    msg = {"message_id": 30, "message_thread_id": TOPIC, "is_topic_message": True,
           "chat": FORUM, "text": "/task", "reply_to_message": target}
    assert handlers.reply_of(msg) == target


# -------------------------------------------------------- /task из чата
def _route_group(monkeypatch: pytest.MonkeyPatch, msg: dict[str, Any], *,
                 projects: int = 1) -> dict[str, list[Any]]:
    """Прогнать сообщение через `on_message`, подменив базу и портал."""
    seen: dict[str, list[Any]] = {"context": [], "create": [], "survey": []}

    async def load(chat_id: int, thread_id: int | None = None) -> context.ChatContext:
        seen["context"].append(thread_id)
        return _ctx(msg["chat"], thread_id, projects)

    async def create_from(ctx: context.ChatContext, source: dict[str, Any],
                          tg_user_id: int, trigger: int | None) -> handlers.Reply:
        seen["create"].append(source)
        return handlers.Reply("создано")

    async def survey_start(ctx: context.ChatContext, tg_user_id: int) -> handlers.Reply:
        seen["survey"].append(tg_user_id)
        return handlers.Reply(texts.MSG_SURVEY_CHOOSE)

    async def no_session(chat_ref: int, thread_id: int | None,
                         owner_tg_id: int) -> None:
        return None

    monkeypatch.setattr(handlers, "load_chat_context", load)
    monkeypatch.setattr(handlers, "_create_from", create_from)
    monkeypatch.setattr(handlers, "_survey_start", survey_start)
    monkeypatch.setattr(handlers.survey, "active_for", no_session)
    asyncio.run(handlers.on_message(BOT, msg))
    return seen


def test_task_reply_in_a_plain_supergroup_takes_the_quoted_message(
        monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _route_group(monkeypatch, PLAIN_TASK_REPLY)
    assert seen["create"] == [REQUEST]
    assert seen["context"] == [None], "начало цепочки ответов — не тема форума"


def test_task_with_text_in_a_forum_topic_uses_the_text(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """В теме форума реплай есть у каждого сообщения — на создание темы.

    Приняв его за источник, бот отвечал «не вижу текста для задачи» на команду,
    в которой текст написан прямо после неё.
    """
    msg = {"message_id": 30, "message_thread_id": TOPIC, "is_topic_message": True,
           "from": BOB, "chat": FORUM, "text": "/task Не открывается форма записи",
           "entities": COMMAND, "reply_to_message": TOPIC_CREATED}
    seen = _route_group(monkeypatch, msg)
    assert [s["text"] for s in seen["create"]] == ["Не открывается форма записи"]
    assert seen["context"] == [TOPIC]


def test_bare_task_in_a_forum_topic_starts_the_survey(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`/task` без текста и без реплая — это опросник (T3), и в теме тоже."""
    msg = {"message_id": 30, "message_thread_id": TOPIC, "is_topic_message": True,
           "from": BOB, "chat": FORUM, "text": "/task", "entities": COMMAND,
           "reply_to_message": TOPIC_CREATED}
    seen = _route_group(monkeypatch, msg)
    assert seen["create"] == []
    assert seen["survey"] == [BOB["id"]]


def test_mention_without_a_reply_in_a_topic_creates_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Упоминание бота без реплая — не триггер T1, и в теме форума тоже."""
    msg = {"message_id": 30, "message_thread_id": TOPIC, "is_topic_message": True,
           "from": BOB, "chat": FORUM, "text": "@devon_sd_bot привет",
           "entities": [{"type": "mention", "offset": 0, "length": 13}],
           "reply_to_message": TOPIC_CREATED}
    seen = _route_group(monkeypatch, msg)
    assert seen["create"] == []


# ------------------------------------------------- ответ на вопрос опросника
class _Sessions:
    """Сессии опросника так, как их хранит база: `thread_id or 0` в ключе."""

    def __init__(self, thread_id: int | None, question_message_id: int) -> None:
        self.session = survey.Session(
            id=9, tenant_id=1, chat_ref=5, thread_id=thread_id or 0,
            owner_tg_id=BOB["id"], template_id=3, project_id=11, step=0,
            answers={}, last_message_id=question_message_id)
        self.recorded: list[str] = []

    async def active_for(self, chat_ref: int, thread_id: int | None,
                         owner_tg_id: int) -> survey.Session | None:
        match = (chat_ref, thread_id or 0, owner_tg_id) == (
            self.session.chat_ref, self.session.thread_id, self.session.owner_tg_id)
        return self.session if match else None

    async def questions(self, tenant_id: int, template_id: int) -> list[survey.Question]:
        return [survey.Question(code="what", text="Что случилось?", required=True)]

    async def record(self, session: survey.Session, code: str, value: str) -> None:
        self.recorded.append(value)


def _answer(monkeypatch: pytest.MonkeyPatch, sessions: _Sessions,
            msg: dict[str, Any]) -> list[str]:
    async def load(chat_id: int, thread_id: int | None = None) -> context.ChatContext:
        return _ctx(msg["chat"], thread_id)

    async def survey_next(*args: Any) -> handlers.Reply:
        return handlers.Reply("следующий вопрос")

    monkeypatch.setattr(handlers, "load_chat_context", load)
    monkeypatch.setattr(handlers.survey, "active_for", sessions.active_for)
    monkeypatch.setattr(handlers.survey, "questions", sessions.questions)
    monkeypatch.setattr(handlers.survey, "record", sessions.record)
    monkeypatch.setattr(handlers, "_survey_next", survey_next)
    asyncio.run(handlers.on_message(BOT, msg))
    return sessions.recorded


def test_survey_answer_by_reply_in_a_plain_supergroup_is_accepted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Опрос начат кнопкой под сообщением без темы — сессия без темы.

    Ответ реплаем на вопрос приходит с `message_thread_id` = id вопроса (начало
    цепочки). Ключуйся сессия им — бот не узнал бы ответ на свой же вопрос и
    молчал, а задача из опросника не создалась бы никогда.
    """
    question = {"message_id": 131, "from": BOT_USER, "chat": PLAIN,
                "text": "Вопрос 1 из 3\n\nЧто случилось?"}
    msg = {"message_id": 132, "message_thread_id": 131, "from": BOB, "chat": PLAIN,
           "text": "Форма записи не открывается", "reply_to_message": question}
    recorded = _answer(monkeypatch, _Sessions(None, 131), msg)
    assert recorded == ["Форма записи не открывается"]


def test_survey_answer_in_a_forum_topic_is_still_accepted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """В теме форума сессия ключуется темой — так было и так осталось."""
    question = {"message_id": 131, "message_thread_id": TOPIC, "is_topic_message": True,
                "from": BOT_USER, "chat": FORUM, "text": "Что случилось?"}
    msg = {"message_id": 132, "message_thread_id": TOPIC, "is_topic_message": True,
           "from": BOB, "chat": FORUM, "text": "Форма записи не открывается",
           "reply_to_message": question}
    recorded = _answer(monkeypatch, _Sessions(TOPIC, 131), msg)
    assert recorded == ["Форма записи не открывается"]


def test_button_under_a_topic_message_opens_the_topic_context(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Кнопка под сообщением бота берёт тему из того же определения, что и текст."""
    threads: list[int | None] = []

    async def consume(token: str, tg_user_id: int) -> dict[str, Any]:
        return {"tenant_id": 1, "kind": "survey_skip", "payload": {"session_id": 9}}

    async def load(chat_id: int, thread_id: int | None = None) -> context.ChatContext:
        threads.append(thread_id)
        return _ctx(FORUM, thread_id)

    async def active_for(chat_ref: int, thread_id: int | None,
                         owner_tg_id: int) -> None:
        threads.append(thread_id)

    monkeypatch.setattr(handlers, "consume_token", consume)
    monkeypatch.setattr(handlers, "load_chat_context", load)
    monkeypatch.setattr(handlers.survey, "active_for", active_for)
    for message, expected in (
            ({"message_id": 131, "message_thread_id": TOPIC, "is_topic_message": True,
              "chat": FORUM}, TOPIC),
            # Сообщение бота, которое само оказалось в цепочке ответов обычной
            # группы: номер цепочки — не тема, и сессии под ним не бывает.
            ({"message_id": 131, "message_thread_id": 119, "chat": PLAIN}, None)):
        threads.clear()
        click = {"data": "k:token", "from": BOB, "message": message}
        asyncio.run(handlers.on_callback(BOT, click))
        assert threads == [expected, expected]


# ------------------------------------------------------- отправка ответа
class _Telegram:
    """Bot API в той части, что здесь важна: чужой `message_thread_id` — отказ.

    Сервер Bot API проверяет, что сообщение с этим номером — начало темы, и иначе
    отвечает «message thread not found». Тем в форуме одна, в обычной группе
    их нет вовсе.
    """

    def __init__(self) -> None:
        self.delivered: list[tuple[int, int | None, str]] = []

    async def send_message(self, token: str, chat_id: int, text: str, *,
                           thread_id: int | None = None,
                           reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        topics = {TOPIC} if chat_id == FORUM["id"] else set()
        if thread_id and thread_id not in topics:
            raise tg.TelegramError(400, "Bad Request: message thread not found")
        self.delivered.append((chat_id, thread_id, text))
        return {"message_id": 500}


def _deliver(monkeypatch: pytest.MonkeyPatch,
             msg: dict[str, Any]) -> list[tuple[int, int | None, str]]:
    telegram = _Telegram()

    async def bot_row(bot_ref: int) -> dict[str, Any]:
        return BOT

    async def not_blocked(tenant_id: int) -> bool:
        return False

    async def on_message(bot: dict[str, Any], m: dict[str, Any]) -> handlers.Reply:
        return handlers.Reply("✅ Задача #240 создана")

    monkeypatch.setattr(dispatch, "_bot_row", bot_row)
    monkeypatch.setattr(lifecycle, "blocked", not_blocked)
    monkeypatch.setattr(dispatch.handlers, "on_message", on_message)
    monkeypatch.setattr(tg, "send_message", telegram.send_message)
    asyncio.run(dispatch.route(1, {"update_id": 1, "message": msg}))
    return telegram.delivered


def test_answer_to_a_reply_in_a_plain_supergroup_reaches_the_chat(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Та самая жалоба: задача создана, а ответ о ней потерялся по дороге в чат."""
    delivered = _deliver(monkeypatch, PLAIN_TASK_REPLY)
    assert delivered == [(PLAIN["id"], None, "✅ Задача #240 создана")]


def test_answer_in_a_forum_topic_stays_in_the_topic(
        monkeypatch: pytest.MonkeyPatch) -> None:
    msg = {"message_id": 30, "message_thread_id": TOPIC, "is_topic_message": True,
           "from": BOB, "chat": FORUM, "text": "/task", "entities": COMMAND,
           "reply_to_message": {"message_id": 29, "message_thread_id": TOPIC,
                                "is_topic_message": True, "from": ALICE,
                                "chat": FORUM, "text": "Не грузится кабинет"}}
    delivered = _deliver(monkeypatch, msg)
    assert delivered == [(FORUM["id"], TOPIC, "✅ Задача #240 создана")]


# ------------------------------------------------------------------ страж
# Два ключа апдейта, каждый из которых значит не то, чем кажется. Читать их мимо
# `topic_of` и `reply_of` — значит вернуть этот баг в новом обработчике.
GUARDED = {"message_thread_id", "reply_to_message"}
ALLOWED = {
    ("bot/handlers.py", "topic_of"),
    ("bot/handlers.py", "reply_of"),
    # Имя параметра ИСХОДЯЩЕГО вызова: тема приходит сюда уже разобранной.
    ("tg/api.py", "send_message"),
}


def _guarded_reads() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Constant) and node.value in GUARDED:
                    found.add((rel, func.name))
    return found


def test_thread_and_reply_are_read_in_one_place_each() -> None:
    assert _guarded_reads() <= ALLOWED


def test_the_guard_sees_the_sites_it_allows() -> None:
    """Страж, который ничего не находит, не отличим от сломанного."""
    assert _guarded_reads() == ALLOWED
