"""Списание времени: разбор длительности, форма вызова портала, честность списка.

Проверяется то, что ломается тихо:

* «2» вместо «2м» — ошибка в шестьдесят раз, если голое число считать часами;
* `MINUTES` в `ARFIELDS` портал отвергает (`ERROR_CORE`), а поле с тем же именем
  в ответе есть — соблазн переиспользовать имя ответа проверяется тестом, а не
  памятью (docs/00-portal-facts.md §5.3);
* `esc_bbcode` в `COMMENT_TEXT` испортил бы текст: поле не BBCode;
* неполный список списаний, показанный как полный, — это неверная сумма
  с уверенным видом.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from b24bot.b24 import errors, mapping
from b24bot.bot import handlers, keyboards, texts
from b24bot.domain import context, timelog


# ------------------------------------------------------------ разбор времени
@pytest.mark.parametrize(("text", "seconds"), [
    ("1ч30м", 5400),
    ("1ч 30м", 5400),
    ("1:30", 5400),
    ("90м", 5400),
    ("90", 5400),          # голое число — минуты
    ("1.5ч", 5400),
    ("1,5ч", 5400),        # запятая как разделитель — обычное дело в русской раскладке
    ("2ч", 7200),
    ("2h", 7200),
    ("45мин", 2700),
    ("45 min", 2700),
    ("0:30", 1800),
    ("8ч", 28800),
])
def test_duration_forms(text: str, seconds: int) -> None:
    assert timelog.parse_duration(text) == seconds


def test_bare_number_is_minutes_not_hours() -> None:
    """Голое число — минуты.

    Опечатка в один символ («2» вместо «2м») при трактовке часами завышала бы
    списание в шестьдесят раз, а завышенные трудозатраты у клиента дороже
    заниженных: их оспаривают, а не прощают.
    """
    assert timelog.parse_duration("2") == 120


@pytest.mark.parametrize("text", [
    "", "   ", "полчаса", "1ч30", "abc", "1ч abc", "-30м", "1:xx", "ч",
    "0", "0м",              # ноль — не списание
    "30с",                  # секунды не принимаем: их не списывают руками
    "25ч",                  # больше суток за раз
    "1440м1м",              # то же самое, но по частям
])
def test_bad_durations_are_refused(text: str) -> None:
    with pytest.raises(timelog.BadDuration):
        timelog.parse_duration(text)


def test_refusal_text_is_shown_to_a_human() -> None:
    """Текст исключения уезжает в чат, поэтому он про дело, а не про код."""
    with pytest.raises(timelog.BadDuration) as exc:
        timelog.parse_duration("30с")
    assert "не понял" in str(exc.value).lower()

    with pytest.raises(timelog.BadDuration) as too_much:
        timelog.parse_duration("25ч")
    assert "24" in str(too_much.value)


# --------------------------------------------------------------- вызов портала
class FakeClient:
    def __init__(self, result: Any = 187) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   **_: Any) -> Any:
        self.calls.append((method, params or {}))
        return self.result


async def test_add_sends_seconds_and_never_minutes() -> None:
    """`ARFIELDS[MINUTES]` портал отвергает — проверено на живом портале.

    Поле `MINUTES` есть в ОТВЕТЕ `getlist`, и именно поэтому здесь стоит тест:
    имена полей ответа нельзя переиспользовать для запроса (§1 фактов портала).
    """
    client = FakeClient()
    entry_id = await timelog.add(client, 233, 5400)  # type: ignore[arg-type]

    assert entry_id == 187
    method, params = client.calls[0]
    assert method == "task.elapseditem.add"
    assert params["TASKID"] == 233
    assert params["ARFIELDS"]["SECONDS"] == 5400
    assert "MINUTES" not in params["ARFIELDS"]


async def test_comment_goes_unescaped_because_the_field_is_not_bbcode() -> None:
    """Скобки уезжают как есть: портал их в этом поле не разбирает.

    `esc_bbcode` здесь заменил бы `[` на полноширинную скобку и испортил текст,
    который никто не собирался разбирать как разметку.
    """
    client = FakeClient()
    await timelog.add(client, 233, 600,  # type: ignore[arg-type]
                      comment="разбор [логов] и BBCode")
    assert client.calls[0][1]["ARFIELDS"]["COMMENT_TEXT"] == "разбор [логов] и BBCode"


async def test_empty_comment_is_not_sent_at_all() -> None:
    client = FakeClient()
    await timelog.add(client, 233, 600)  # type: ignore[arg-type]
    assert "COMMENT_TEXT" not in client.calls[0][1]["ARFIELDS"]


async def test_started_at_is_sent_only_when_given() -> None:
    client = FakeClient()
    await timelog.add(client, 233, 600,  # type: ignore[arg-type]
                      started_at="2026-08-27T10:00:00+03:00")
    assert client.calls[0][1]["ARFIELDS"]["DATE_START"] == "2026-08-27T10:00:00+03:00"


async def test_unexpected_result_is_an_error_not_a_shrug() -> None:
    """Метод отдаёт голый ID. Что угодно другое — портал изменился.

    Промолчать нельзя: списание могло и пройти, и не пройти, и «тихий успех»
    здесь означал бы потерянные часы, о которых никто не узнает.
    """
    client = FakeClient(result={"unexpected": True})
    with pytest.raises(errors.B24Error):
        await timelog.add(client, 233, 600)  # type: ignore[arg-type]


# ------------------------------------------------------------- список задачи
def _raw(entry_id: int, task_id: int, seconds: int, user: int = 1,
         comment: str = "") -> dict[str, Any]:
    return {"ID": str(entry_id), "TASK_ID": str(task_id), "USER_ID": str(user),
            "SECONDS": str(seconds), "MINUTES": str(seconds // 60),
            "COMMENT_TEXT": comment, "DATE_START": "2026-08-27T10:00:00+03:00"}


class ListClient:
    """Портал: один и тот же список с обоих концов, как на малых объёмах."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   **_: Any) -> Any:
        return self.items


async def test_entries_of_other_tasks_are_not_counted() -> None:
    client = ListClient([_raw(1, 233, 3600), _raw(3, 999, 7200),
                         _raw(5, 233, 1800)])
    result = await timelog.for_task(client, 233, 5400)  # type: ignore[arg-type]

    assert [e.id for e in result.entries] == [5, 1]  # свежие сверху
    assert sum(e.seconds for e in result.entries) == 5400
    assert result.complete


async def test_incomplete_list_is_admitted() -> None:
    """Сумма видимого меньше суммы задачи — значит часть за окном выборки.

    Полнота доказывается вычитанием, а не размером выборки: у метода портала нет
    ни фильтров, ни страниц, и «показали 50» ничего не говорит о том, все ли это.
    """
    client = ListClient([_raw(1, 233, 3600)])
    result = await timelog.for_task(client, 233, 36000)  # type: ignore[arg-type]

    assert not result.complete
    assert result.total_seconds == 36000, "сумма берётся у задачи, а не у списка"


async def test_portal_failure_leaves_the_sum_from_the_task() -> None:
    """Список не прочитался — сумма задачи всё равно верна и показывается."""
    class Broken:
        async def call(self, *_: object, **__: object) -> Any:
            raise errors.B24Error("ERROR_CORE", "нет", "task.elapseditem.getlist")

    result = await timelog.for_task(Broken(), 233, 7200)  # type: ignore[arg-type]
    assert result.entries == []
    assert result.total_seconds == 7200
    assert not result.complete


# ------------------------------------------------------------- разбор команды
def _msg(**kw: Any) -> dict[str, Any]:
    return {"message_id": 10, "chat": {"id": -100}, **kw}


def test_command_parses_number_duration_and_comment() -> None:
    data = handlers.time_input("233 1ч30м починил интеграцию", _msg())
    assert (data.task_id, data.seconds) == (233, 5400)
    assert data.comment == "починил интеграцию"
    assert not data.error


def test_command_without_duration_asks_for_usage() -> None:
    assert handlers.time_input("233", _msg()).error == "usage"
    assert handlers.time_input("", _msg()).error == "usage"


def test_bad_duration_keeps_the_task_number_and_names_the_reason() -> None:
    data = handlers.time_input("233 полчаса", _msg())
    assert data.task_id == 233
    assert data.seconds is None
    assert data.error and data.error != "usage"


def test_reply_becomes_the_comment_when_none_is_typed() -> None:
    """Тот же жест, что у `/comment`: ответить на реплику вместо переписывания."""
    reply = {"text": "чинили выгрузку два часа",
             "from": {"first_name": "Пётр", "last_name": "Иванов"}}
    data = handlers.time_input("233 2ч", _msg(reply_to_message=reply))
    assert data.comment == "чинили выгрузку два часа"
    assert "Пётр" in data.quoted_author


def test_typed_comment_wins_over_the_reply() -> None:
    reply = {"text": "чужая реплика", "from": {"first_name": "Пётр"}}
    data = handlers.time_input("233 2ч свой текст", _msg(reply_to_message=reply))
    assert data.comment == "свой текст"
    assert data.quoted_author == ""


def test_empty_reply_is_not_a_quote() -> None:
    """В форуме Telegram сам подставляет ответ на служебное сообщение о топике.

    Приняв его за цитату, мы приписали бы комментарий тому, кто завёл топик,
    и не сказали бы ни слова о содержимом.
    """
    data = handlers.time_input("233 2ч", _msg(reply_to_message={"from": {}}))
    assert data.comment == ""
    assert data.quoted_author == ""


# ------------------------------------------------------------------- кнопки
def test_presets_are_all_parseable_back() -> None:
    """Кнопка отправляет ту же строку, что и человек руками.

    Разбор один на все двери: разойдись он, «1 ч» с кнопки и «1ч» из команды
    однажды дали бы разные минуты, и заметить это было бы нечем.
    """
    for seconds in timelog.PRESETS:
        assert timelog.parse_duration(str(seconds // 60)) == seconds
        assert timelog.preset_label(seconds)


def test_checked_rejects_what_parse_duration_rejects() -> None:
    """Мини-апп присылает готовые секунды числом — границы обязаны быть те же."""
    assert timelog.checked(3600) == 3600
    with pytest.raises(timelog.BadDuration):
        timelog.checked(30)
    with pytest.raises(timelog.BadDuration):
        timelog.checked(25 * 3600)


# ------------------------------------------------------- дверь к списанию
# Всё, что ниже, — про один урок: списание времени было сделано, но человек его
# не находил. Кнопка карточки пряталась по ключу, которого в ответе портала могло
# и не быть; списка задач с кнопкой не было вовсе; в личке двери не было совсем.
def test_card_shows_the_time_button_when_the_portal_says_nothing() -> None:
    """Отсутствие ключа — это «портал не сказал», а не «нельзя».

    Блок `action` надёжен для запретов и не исчерпывающий для разрешений
    (docs/00-portal-facts.md §9.5). У завершения и правки есть второй путь —
    портал и приложение; у списания времени эта кнопка была единственной
    дверью, и пропавший ключ прятал её целиком: человек видел не «нельзя»,
    а «такой функции нет».
    """
    assert "⏱ Списать время" in _card_labels(allowed={"complete"}, forbidden=set())


def test_card_hides_the_time_button_only_on_an_explicit_refusal() -> None:
    assert "⏱ Списать время" not in _card_labels(
        allowed={"complete", "elapsedtime.add"}, forbidden={"elapsedtime.add"})


def test_forbidden_and_allowed_are_read_from_the_same_block() -> None:
    """`false` и «ключа нет» обязаны различаться — на этом стоит правило выше."""
    task = {"action": {"complete": True, "elapsedtime.add": False, "edit": None}}
    assert mapping.allowed_actions(task) == {"complete"}
    assert mapping.forbidden_actions(task) == {"elapsedtime.add"}
    assert mapping.forbidden_actions({"action": {}}) == set()
    assert mapping.forbidden_actions({}) == set()


def _card_labels(*, allowed: set[str], forbidden: set[str]) -> list[str]:
    tokens = {"complete": "c", "refresh": "r", "timelog": "tl", "back": "b"}
    markup = keyboards.task_card(tokens, allowed=allowed, forbidden=forbidden,
                                 portal_url="https://p.example/task/1/")
    return [b["text"] for row in markup["inline_keyboard"] for b in row]


def test_pick_puts_one_button_under_each_task() -> None:
    """Кнопка под каждой задачей — то, о чём просили: номер наизусть не помнят."""
    items = [("t1", "⏱ #233"), ("t2", "⏱ #234"), ("t3", "⏱ #235"), ("t4", "⏱ #236")]
    rows = keyboards.timelog_pick(items)["inline_keyboard"]
    assert [b["text"] for row in rows for b in row] == [label for _t, label in items]
    assert all(b["callback_data"].startswith("tl:") for row in rows for b in row)
    assert all(len(row) <= 3 for row in rows), "четыре в ряд на телефоне режутся"


def test_pick_in_private_goes_to_its_own_branch() -> None:
    """В личке ChatContext взять неоткуда, и разбор идёт до его загрузки.

    Общий префикс означал бы, что кнопку из чата можно отправить в ветку,
    которая проверяет права по теннанту, а не по привязке чата.
    """
    rows = keyboards.timelog_pick([("t1", "⏱ #233")], private=True)["inline_keyboard"]
    assert rows[0][0]["callback_data"].startswith("tm:")


def test_private_keyboard_has_a_visible_door() -> None:
    """Списание времени — действие, а не отчёт: у него своя кнопка и своё слово."""
    labels = [b["text"] for row in keyboards.persistent_private()["keyboard"]
              for b in row]
    assert "⏱ Списать время" in labels
    assert keyboards.PRIVATE_LABELS["⏱ Списать время"] == "timelog"


def test_the_old_report_label_still_works() -> None:
    """Клавиатура у человека обновится только с нашим следующим ответом.

    До тех пор он жмёт ту кнопку, что стоит у него на экране, и «бот не
    реагирует» — это ровно то, чего стоит один переименованный ярлык.
    """
    assert keyboards.PRIVATE_LABELS["⏱ Трудозатраты"] == "timesheet"
    assert keyboards.PRIVATE_LABELS["📈 Трудозатраты"] == "timesheet"


def test_time_command_without_a_number_offers_the_list(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`/time` без номера — не ошибка формата, а самый частый способ им пользоваться."""
    seen: list[str] = []

    async def pick(ctx: object, tg_user_id: int, b24_user_id: int) -> handlers.Reply:
        seen.append("pick")
        return handlers.Reply("список")

    async def linked(tenant_id: int, tg_user_id: int) -> int:
        return 42

    monkeypatch.setattr(handlers, "_timelog_pick", pick)
    monkeypatch.setattr(handlers.access, "linked_b24_user", linked)
    reply = asyncio.run(handlers._time_command(_chat_ctx(), _msg(), "", 77))
    assert seen == ["pick"]
    assert reply.text == "список"


def test_time_command_with_a_number_opens_that_task(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Спросили про конкретную задачу — открываем её экран, а не читаем формат."""
    seen: list[dict[str, Any]] = []

    async def screen(ctx: object, tg_user_id: int,
                     payload: dict[str, Any]) -> handlers.Reply:
        seen.append(payload)
        return handlers.Reply("экран задачи")

    async def linked(tenant_id: int, tg_user_id: int) -> int:
        return 42

    monkeypatch.setattr(handlers, "_timelog", screen)
    monkeypatch.setattr(handlers.access, "linked_b24_user", linked)
    reply = asyncio.run(handlers._time_command(_chat_ctx(), _msg(), "233", 77))
    assert seen == [{"task_id": 233, "act": "menu"}]
    assert reply.text == "экран задачи"


def test_bad_duration_is_still_a_refusal_not_a_screen(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """«/time 233 полчаса» — это опечатка, и молча открывать экран нельзя."""
    async def linked(tenant_id: int, tg_user_id: int) -> int:
        return 42

    monkeypatch.setattr(handlers.access, "linked_b24_user", linked)
    reply = asyncio.run(handlers._time_command(_chat_ctx(), _msg(), "233 полчаса", 77))
    assert "Не понял" in reply.text


def test_pick_falls_back_to_all_open_tasks_and_says_so(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой ответ на «списать время» читался бы как поломка.

    На человека может быть не назначено ничего — но списывают время и в чужие
    задачи, поэтому список не схлопывается, а меняет заголовок и объясняет себя.
    """
    reply = asyncio.run(_run_pick(monkeypatch, responsible=999))
    assert texts.MSG_TIMELOG_PICK_NONE_MINE in reply.text
    assert "⏱ Открытые задачи" in reply.text
    labels = [b["text"] for row in reply.markup["inline_keyboard"] for b in row]
    assert labels == ["⏱ #233", "⏱ #234"]


def test_pick_shows_my_tasks_first(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = asyncio.run(_run_pick(monkeypatch, responsible=42))
    assert texts.MSG_TIMELOG_PICK_NONE_MINE not in reply.text
    assert "⏱ Мои задачи" in reply.text


def test_pick_says_when_there_is_nothing_at_all(
        monkeypatch: pytest.MonkeyPatch) -> None:
    reply = asyncio.run(_run_pick(monkeypatch, responsible=42, tasks=[]))
    assert reply.markup is None
    assert "/time" in reply.text, "путь по номеру остаётся и для закрытых задач"


async def _run_pick(monkeypatch: pytest.MonkeyPatch, *, responsible: int,
                    tasks: list[dict[str, Any]] | None = None) -> handlers.Reply:
    """Прогон `/time` без номера с подменённым порталом. До базы дело не доходит."""
    if tasks is None:
        tasks = [{"id": 233, "title": "Лид-форма", "status": 3,
                  "responsibleId": str(responsible)},
                 {"id": 234, "title": "Выгрузка", "status": 2,
                  "responsibleId": str(responsible)}]

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def client_for_user(tenant_id: int, b24_user_id: int,
                              actor_tg_user_id: int | None = None) -> _Client:
        return _Client()

    async def fetch_open(client: object, group_ids: list[int]) -> list[dict[str, Any]]:
        return tasks

    async def domain(tenant_id: int) -> str:
        return "devondev.bitrix24.ru"

    async def token(ctx: object, tg_user_id: int, task_id: int, act: str,
                    seconds: int | None = None) -> str:
        return f"tok{task_id}"

    monkeypatch.setattr(handlers.access, "client_for_user", client_for_user)
    monkeypatch.setattr(handlers.views, "fetch_open", fetch_open)
    monkeypatch.setattr(handlers, "_tenant_domain", domain)
    monkeypatch.setattr(handlers, "_timelog_token", token)
    return await handlers._timelog_pick(_chat_ctx(), 77, 42)


def _chat_ctx() -> context.ChatContext:
    return context.ChatContext(
        chat_ref=5, chat_id=-100500, title="Поддержка", status="active",
        tenant_id=1, is_forum=False,
        projects=[context.ProjectRef(11, 101, "Devon SD BOT", "Линия Жизни")])


def test_private_button_is_parsed_before_the_chat_context(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Личных чатов нет в `tg_chats`, и контекст там всегда пуст.

    Разбирайся `tm` после его загрузки — кнопка отвечала бы «чат не подключён»
    в диалоге, который сама же и завела. Та же причина, по которой до контекста
    разбираются подтверждение задач и личный отчёт.
    """
    seen: list[dict[str, Any]] = []

    async def consume(token: str, actor: int | None) -> dict[str, Any]:
        return {"kind": "timelog_dm", "tenant_id": 1, "owner_tg_id": 77,
                "chat_ref": None, "payload": {"task_id": 233, "act": "menu"}}

    async def no_context(chat_id: int, thread_id: int | None = None) -> None:
        return None

    async def dm(tenant_id: int, tg_user_id: int,
                 payload: dict[str, Any]) -> handlers.Reply:
        seen.append(payload)
        return handlers.Reply("экран списания")

    monkeypatch.setattr(handlers, "consume_token", consume)
    monkeypatch.setattr(handlers, "load_chat_context", no_context)
    monkeypatch.setattr(handlers, "_dm_timelog", dm)

    click = {"data": "tm:token", "from": {"id": 77},
             "message": {"message_id": 1, "chat": {"id": 77, "type": "private"}}}
    reply = asyncio.run(handlers.on_callback({}, click))
    assert seen == [{"task_id": 233, "act": "menu"}]
    assert reply is not None and reply.text == "экран списания"


def test_chat_token_cannot_be_replayed_in_the_private_branch() -> None:
    """Ветки проверяют доступ по-разному: чат — по привязке, личка — по теннанту.

    Общий вид токена означал бы, что кнопку из чата можно отправить в ветку,
    где проверка другая, и выглядела бы она выполненной.
    """
    from b24bot.bot import callbacks

    assert not callbacks.accepts("tm", "timelog")
    assert not callbacks.accepts("tl", "timelog_dm")
    assert callbacks.accepts("tm", "timelog_dm")


def test_private_screen_refuses_a_task_outside_the_tenant(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Граница И-3 в личке: чужая задача отвечает тем же текстом, что и любая другая."""
    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def linked(tenant_id: int, tg_user_id: int) -> int:
        return 42

    async def client_for_user(tenant_id: int, b24_user_id: int,
                              actor_tg_user_id: int | None = None) -> _Client:
        return _Client()

    async def read(client: object, task_id: int) -> dict[str, Any]:
        return {"id": task_id, "title": "Чужая", "groupId": 33}

    async def refuse(tenant_id: int, task_id: int,
                     group_id_hint: int | None = None) -> None:
        return None

    async def added(*args: Any, **kw: Any) -> int:
        raise AssertionError("списание в чужую задачу не должно доехать до портала")

    monkeypatch.setattr(handlers.access, "linked_b24_user", linked)
    monkeypatch.setattr(handlers.access, "client_for_user", client_for_user)
    monkeypatch.setattr(handlers.task_service, "read", read)
    monkeypatch.setattr(handlers, "authorize_task_for_tenant", refuse)
    monkeypatch.setattr(handlers.timelog, "add", added)

    reply = asyncio.run(handlers._dm_timelog(1, 77, {"task_id": 999, "act": "add",
                                                     "seconds": 3600}))
    assert reply.text == texts.MSG_TASK_NOT_FOUND


def test_private_screen_logs_the_time_and_writes_the_journal(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Списание из лички — то же действие и то же имя в журнале, что из чата.

    Разойдись имена, выборка «кто сколько списал» врала бы, ничем себя не выдав:
    записи есть, они просто разные (страж `tests/test_audit_names.py`).
    """
    logged: list[tuple[int, int]] = []
    audited: list[tuple[str, dict[str, Any]]] = []

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def linked(tenant_id: int, tg_user_id: int) -> int:
        return 42

    async def client_for_user(tenant_id: int, b24_user_id: int,
                              actor_tg_user_id: int | None = None) -> _Client:
        return _Client()

    async def read(client: object, task_id: int) -> dict[str, Any]:
        return {"id": task_id, "title": "Лид-форма", "groupId": 33,
                "timeSpentInLogs": "3600"}

    async def allow(tenant_id: int, task_id: int,
                    group_id_hint: int | None = None) -> context.ProjectRef:
        return context.ProjectRef(11, 33, "Devon SD BOT", "Линия Жизни")

    async def add(client: object, task_id: int, seconds: int,
                  **kw: Any) -> int:
        logged.append((task_id, seconds))
        return 1

    async def for_task(client: object, task_id: int, total: int) -> timelog.TaskEntries:
        return timelog.TaskEntries(entries=[], total_seconds=total, complete=True)

    async def names(client: object, ids: list[int]) -> dict[int, str]:
        return {}

    async def record(tenant_id: int, action: str, **kw: Any) -> None:
        audited.append((action, kw))

    async def token(tenant_id: int, tg_user_id: int, task_id: int, act: str,
                    seconds: int | None = None) -> str:
        return "tok"

    monkeypatch.setattr(handlers.access, "linked_b24_user", linked)
    monkeypatch.setattr(handlers.access, "client_for_user", client_for_user)
    monkeypatch.setattr(handlers.task_service, "read", read)
    monkeypatch.setattr(handlers.task_service, "user_names", names)
    monkeypatch.setattr(handlers, "authorize_task_for_tenant", allow)
    monkeypatch.setattr(handlers.timelog, "add", add)
    monkeypatch.setattr(handlers.timelog, "for_task", for_task)
    monkeypatch.setattr(handlers.audit, "record", record)
    monkeypatch.setattr(handlers, "_dm_timelog_token", token)

    reply = asyncio.run(handlers._dm_timelog(1, 77, {"task_id": 233, "act": "add",
                                                     "seconds": 3600}))
    assert logged == [(233, 3600)]
    assert audited and audited[0][0] == timelog.AUDIT_ACTION
    assert "списано" in reply.text
    labels = [b["text"] for row in reply.markup["inline_keyboard"] for b in row]
    assert "◀️ К списку" in labels, "из лички возвращаться некуда, кроме списка"


def test_pick_in_an_unbound_chat_names_the_real_problem(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """«Задач не видно» и «чат не привязан» — разные вещи, и второе чинится.

    Область выборки задаёт только `GROUP_ID` (docs/40-security.md §1), поэтому
    пустой список проектов не превращается в «все задачи портала» — но и молчать
    о причине нельзя: человек ищет задачи, а чинить надо привязку.
    """
    async def boom(*args: Any, **kw: Any) -> None:
        raise AssertionError("в чат без привязок ходить в портал незачем")

    monkeypatch.setattr(handlers.access, "client_for_user", boom)
    ctx = context.ChatContext(chat_ref=5, chat_id=-100500, title="Поддержка",
                              status="active", tenant_id=1, is_forum=False,
                              projects=[])
    reply = asyncio.run(handlers._timelog_pick(ctx, 77, 42))
    assert reply.text == texts.MSG_NO_PROJECT


# ------------------------------------------- быстрые кнопки и свободный ввод
# 09.09.2026, по заказчику: часы рабочего дня кнопками, всё остальное — «Другое».
# Прежний набор (15 м, 30 м, 1, 2, 4, 8 ч) не покрывал 3, 5, 6 и 7 часов, то есть
# половину смены нельзя было списать ни одной кнопкой.
def test_quick_buttons_are_the_hours_of_a_working_day() -> None:
    assert [seconds // 3600 for seconds in timelog.PRESETS] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert all(seconds % 3600 == 0 for seconds in timelog.PRESETS)
    assert [timelog.preset_label(s) for s in timelog.PRESETS] == [
        f"{h} ч" for h in range(1, 9)]


def test_fractional_is_hours_because_fractional_minutes_do_not_exist() -> None:
    """Два правила на голое число, и они не спорят.

    Целое — минуты: опечатка в один символ при трактовке часами завышала бы
    списание в шестьдесят раз. Дробное — часы: списаний на полторы минуты не
    бывает (минимум и так минута), а запятую случайно не набирают.
    """
    assert timelog.parse_duration("90") == 5400        # целое — минуты
    assert timelog.parse_duration("1,5") == 5400       # дробное — часы
    assert timelog.parse_duration("1.5") == 5400
    assert timelog.parse_duration("0,5") == 1800
    assert timelog.parse_duration("0,25") == 900       # четверть часа, а не 25 минут
    with pytest.raises(timelog.BadDuration):
        timelog.parse_duration("0,01")                 # 36 секунд — ниже минимума


def test_menu_is_three_rows_of_three_and_the_ninth_is_free_input() -> None:
    items = [(f"t{s}", timelog.preset_label(s)) for s in timelog.PRESETS]
    rows = keyboards.timelog_menu(items, "back", "ask")["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["1 ч", "2 ч", "3 ч"]
    assert [b["text"] for b in rows[2]] == ["7 ч", "8 ч", "✏️ Другое"]
    assert rows[-1][0]["text"] == "◀️ К карточке"
    assert rows[2][2]["callback_data"] == "tl:ask"


def test_free_input_button_lives_in_its_own_branch_in_private() -> None:
    rows = keyboards.timelog_menu([("t", "1 ч")], "back", "ask",
                                  private=True)["inline_keyboard"]
    assert all(b["callback_data"].startswith("tm:") for row in rows for b in row)


def test_ask_opens_a_reply_field_with_a_new_message() -> None:
    """`force_reply` живёт только в `sendMessage`.

    `editMessageText` принимает лишь инлайн-клавиатуру, поэтому приглашение
    обязано ехать НОВЫМ сообщением, а не подменять собой экран списаний.
    """
    reply = asyncio.run(handlers._timelog(_chat_ctx(), 77,
                                          {"task_id": 233, "act": "ask"}))
    assert reply.edit is False
    assert reply.markup == {"force_reply": True,
                            "input_field_placeholder": texts.MSG_TIMELOG_PLACEHOLDER}
    assert "#233" in reply.text


def test_the_invitation_carries_the_task_number_back() -> None:
    """Состояния у свободного ввода нет: номер едет в тексте вопроса.

    Реплай приносит текст обратно — из Telegram уже без разметки. Разъедься
    фраза и выражение разбора, кнопка «Другое» молча перестала бы принимать
    ответы: сообщение уходило бы, а ответ на него никто не узнавал.
    """
    asked = texts.MSG_TIMELOG_ASK.format(task_id=4242)
    assert handlers.timelog_ask_task_id(asked) == 4242
    assert handlers.timelog_ask_task_id(re.sub(r"<[^>]+>", "", asked)) == 4242
    assert handlers.timelog_ask_task_id("просто разговор в чате") is None


_BOT = {"username": "devon_sd_bot", "bot_id": 900, "tenant_id": 1}


def _ask_message(task_id: int = 233, *, author: dict[str, Any] | None = None,
                 ) -> dict[str, Any]:
    return {"message_id": 5, "from": author or {"id": 900, "is_bot": True},
            "text": re.sub(r"<[^>]+>", "",
                           texts.MSG_TIMELOG_ASK.format(task_id=task_id))}


def test_only_our_own_invitation_is_answered() -> None:
    """Тот же текст мог напечатать и человек, а реплай на чужое сообщение —
    обычная реплика в переписке. Съев её, бот отвечал бы в чужой разговор."""
    assert handlers._asked_timelog_task(
        _ask_message(author={"id": 77, "is_bot": False}), _BOT) is None
    assert handlers._asked_timelog_task(
        _ask_message(author={"id": 901, "is_bot": True}), _BOT) is None
    assert handlers._asked_timelog_task(_ask_message(), _BOT) == 233


def test_free_input_goes_through_the_same_write_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Ответ длительностью и кнопка «1 ч» приходят в одну и ту же функцию.

    Вторая дорога к `timelog.add` означала бы вторую проверку прав и второй
    аудит — то есть два места, где их можно забыть по-разному.
    """
    seen: list[dict[str, Any]] = []

    async def spy(ctx: object, tg_user_id: int,
                  payload: dict[str, Any]) -> handlers.Reply:
        seen.append(payload)
        return handlers.Reply("ok")

    monkeypatch.setattr(handlers, "_timelog", spy)
    reply = asyncio.run(handlers._timelog_answer(
        _chat_ctx(), _msg(text="1ч30м"), _ask_message(), 77, _BOT))
    assert reply is not None
    assert seen == [{"task_id": 233, "act": "add", "seconds": 5400}]


def test_a_reply_to_something_else_is_none_not_a_refusal(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """None означает «обрабатывай дальше»: за этой веткой стоит создание задачи
    реплаем с упоминанием, и перехватив его, мы сломали бы главный сценарий."""
    async def boom(*args: Any, **kw: Any) -> None:
        raise AssertionError("до списания дело доходить не должно")

    monkeypatch.setattr(handlers, "_timelog", boom)
    other = {"message_id": 7, "from": {"id": 900, "is_bot": True},
             "text": "📋 Карточка задачи #233"}
    assert asyncio.run(handlers._timelog_answer(
        _chat_ctx(), _msg(text="1ч30м"), other, 77, _BOT)) is None


def test_a_typo_asks_again_and_the_second_answer_is_recognized(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Переспрос повторяет приглашение целиком, вместе с номером задачи.

    Без номера следующий реплай прилетел бы на сообщение, в котором опознавать
    нечего, — и за опечатку человек платил бы возвратом к карточке и повторным
    нажатием «Другое».
    """
    async def boom(*args: Any, **kw: Any) -> None:
        raise AssertionError("неразобранная длительность в портал не уходит")

    monkeypatch.setattr(handlers, "_timelog", boom)
    reply = asyncio.run(handlers._timelog_answer(
        _chat_ctx(), _msg(text="полчасика"), _ask_message(), 77, _BOT))
    assert reply is not None
    assert "полчасика" in reply.text
    assert reply.markup == {"force_reply": True,
                            "input_field_placeholder": texts.MSG_TIMELOG_PLACEHOLDER}
    assert handlers.timelog_ask_task_id(reply.text) == 233


def test_private_free_input_reaches_the_private_branch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """В личке правило ввода то же самое.

    Принимать там «следующее сообщение» было бы можно — собеседник один, — но
    тогда одно и то же сообщение в двух местах понималось бы по-разному, а
    подписи постоянной клавиатуры («45» рядом с «📊 Мои задачи») разбирались бы
    в зависимости от того, чем закончился прошлый экран.
    """
    seen: list[dict[str, Any]] = []

    async def spy(tenant_id: int, tg_user_id: int,
                  payload: dict[str, Any]) -> handlers.Reply:
        seen.append(payload)
        return handlers.Reply("ok")

    async def tenant_of_user(tg_user_id: int) -> int:
        return 1

    monkeypatch.setattr(handlers, "_dm_timelog", spy)
    monkeypatch.setattr(handlers.access, "tenant_of_user", tenant_of_user)
    msg = {"chat": {"id": 77, "type": "private"}, "from": {"id": 77},
           "text": "45", "reply_to_message": _ask_message(task_id=234)}
    reply = asyncio.run(handlers._private(_BOT, None, 77, {}, "45", msg))
    assert reply is not None
    assert seen == [{"task_id": 234, "act": "add", "seconds": 2700}]


def test_private_keyboard_still_works_next_to_free_input(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Реплай на что угодно другое не должен глотать нажатие кнопки."""
    called: list[str] = []

    async def action(name: str, tg_user_id: int) -> handlers.Reply:
        called.append(name)
        return handlers.Reply("ok")

    monkeypatch.setattr(handlers, "_private_action", action)
    msg = {"chat": {"id": 77, "type": "private"}, "from": {"id": 77},
           "text": "⏱ Списать время",
           "reply_to_message": {"message_id": 9, "from": {"id": 900, "is_bot": True},
                                "text": "📋 Карточка задачи #233"}}
    asyncio.run(handlers._private(_BOT, None, 77, {}, "⏱ Списать время", msg))
    assert called == ["timelog"]


# ---------------------------------------- `/time` с аргументами в личке
# Дыра, найденная попутно 10.09.2026: в личке команда разбиралась как «действие
# постоянной клавиатуры» и молча теряла и номер, и длительность — при том что
# список задач сам эту короткую форму и советует.
def _run_dm_time(monkeypatch: pytest.MonkeyPatch, arg: str,
                 msg: dict[str, Any] | None = None) -> tuple[handlers.Reply,
                                                             list[dict[str, Any]],
                                                             list[str]]:
    seen: list[dict[str, Any]] = []
    listed: list[str] = []

    async def dm_timelog(tenant_id: int, tg_user_id: int,
                         payload: dict[str, Any]) -> handlers.Reply:
        seen.append(payload)
        return handlers.Reply("экран")

    async def action(name: str, tg_user_id: int) -> handlers.Reply:
        listed.append(name)
        return handlers.Reply("список")

    async def tenant_of_user(tg_user_id: int) -> int:
        return 1

    monkeypatch.setattr(handlers, "_dm_timelog", dm_timelog)
    monkeypatch.setattr(handlers, "_private_action", action)
    monkeypatch.setattr(handlers.access, "tenant_of_user", tenant_of_user)
    reply = asyncio.run(handlers._private(_BOT, ("time", arg), 77, {}, f"/time {arg}",
                                          msg or {"chat": {"type": "private"}}))
    assert reply is not None
    return reply, seen, listed


def test_private_time_command_logs_the_full_form(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Полная форма списывает, а не показывает список.

    Команда, печатающая своё обещание («быстрее одной командой») и не
    выполняющая его, читается как поломка бота — то же правило, что у меню
    команд: названная и неработающая команда хуже отсутствующей.
    """
    _reply, seen, listed = _run_dm_time(monkeypatch, "233 1ч30м починил интеграцию")
    assert listed == []
    assert seen == [{"task_id": 233, "act": "add", "seconds": 5400,
                     "comment": "починил интеграцию"}]


def test_private_time_command_with_only_a_number_opens_that_task(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _reply, seen, _listed = _run_dm_time(monkeypatch, "233")
    assert seen == [{"task_id": 233, "act": "menu"}]


def test_private_time_command_without_a_number_still_lists(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустая форма — самый частый способ пользоваться командой, и он не изменился."""
    _reply, seen, listed = _run_dm_time(monkeypatch, "")
    assert seen == []
    assert listed == ["timelog"]


def test_private_time_command_names_a_bad_duration(
        monkeypatch: pytest.MonkeyPatch) -> None:
    reply, seen, _listed = _run_dm_time(monkeypatch, "233 полчасика")
    assert seen == [], "неразобранная длительность в портал не уходит"
    assert "полчасика" in reply.text


def test_private_time_command_carries_the_comment_to_the_portal(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Комментарий приезжает из команды и обязан доехать до портала.

    До этой правки `_dm_timelog` звал `timelog.add` вовсе без комментария —
    у кнопки его взять неоткуда, и параметра просто не было.
    """
    written: list[dict[str, Any]] = []

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def linked(tenant_id: int, tg_user_id: int) -> int:
        return 42

    async def client_for_user(tenant_id: int, b24_user_id: int,
                              actor_tg_user_id: int | None = None) -> _Client:
        return _Client()

    async def read(client: object, task_id: int) -> dict[str, Any]:
        return {"id": task_id, "title": "Задача", "groupId": 101,
                "timeSpentInLogs": "5400"}

    async def authorize(tenant_id: int, task_id: int,
                        group_id_hint: int | None = None) -> context.ProjectRef:
        return context.ProjectRef(11, 101, "Devon SD BOT", "Линия Жизни")

    async def add(client: object, task_id: int, seconds: int, *,
                  comment: str = "", started_at: str | None = None) -> int:
        written.append({"task_id": task_id, "seconds": seconds, "comment": comment})
        return 1

    async def for_task(client: object, task_id: int,
                       total: int) -> timelog.TaskEntries:
        return timelog.TaskEntries([], total, complete=True)

    async def names(client: object, ids: list[int]) -> dict[int, str]:
        return {}

    async def record(*args: Any, **kw: Any) -> None:
        return None

    async def token(tenant_id: int, tg_user_id: int, task_id: int, act: str,
                    seconds: int | None = None) -> str:
        return "tok"

    monkeypatch.setattr(handlers.access, "linked_b24_user", linked)
    monkeypatch.setattr(handlers.access, "client_for_user", client_for_user)
    monkeypatch.setattr(handlers.task_service, "read", read)
    monkeypatch.setattr(handlers.task_service, "user_names", names)
    monkeypatch.setattr(handlers, "authorize_task_for_tenant", authorize)
    monkeypatch.setattr(handlers.timelog, "add", add)
    monkeypatch.setattr(handlers.timelog, "for_task", for_task)
    monkeypatch.setattr(handlers.audit, "record", record)
    monkeypatch.setattr(handlers, "_dm_timelog_token", token)

    asyncio.run(handlers._dm_timelog(1, 77, {"task_id": 233, "act": "add",
                                             "seconds": 5400,
                                             "comment": "разбор логов"}))
    assert written == [{"task_id": 233, "seconds": 5400, "comment": "разбор логов"}]
