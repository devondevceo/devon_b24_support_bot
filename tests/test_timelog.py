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

from typing import Any

import pytest

from b24bot.b24 import errors
from b24bot.bot import handlers
from b24bot.domain import timelog


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
