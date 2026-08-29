"""Тег поддержки: пометка задач и разрез трудозатрат по нему.

Проверяется то, что ломается тихо и правдоподобно:

* дозапись тега, стирающая ключ идемпотентности, — повтор того же сообщения
  создал бы вторую задачу, и заметили бы это по дублям, а не по тесту (И-10);
* отчёт, показывающий одну сумму вместо двух, — падение первой читается как
  потеря данных, хотя означает работу мимо бота;
* пустой отчёт без второй строки — «работы не было» вместо «работу вели мимо нас».
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from b24bot.bot import views
from b24bot.bot.task_create import Draft, _tags
from b24bot.domain import support_tag, timesheet
from b24bot.domain.context import ProjectRef

PROJECT = ProjectRef(id=5, b24_group_id=33, name="Поддержка", client_name="Клиент")
TAG = "tg-support"


# ------------------------------------------------------------------- разбор
def test_normalize_trims_and_collapses_spaces() -> None:
    assert support_tag.normalize("  tg-support  ") == "tg-support"
    assert support_tag.normalize("тег поддержки") == "тег поддержки"


def test_empty_tag_is_a_legal_answer() -> None:
    """Пустая строка — это «не помечать задачи», а не «не настроено».

    Отличать «не настроено» от «выключено» на верхнем уровне цепочки И-9 незачем:
    выше теннанта наследовать не у кого.
    """
    assert support_tag.normalize("") == ""
    assert support_tag.normalize("   ") == ""


@pytest.mark.parametrize("bad", ["a,b", "a;b", "x" * 61])
def test_bad_tags_are_refused(bad: str) -> None:
    """Запятая режет тег надвое везде, где Битрикс показывает теги списком."""
    with pytest.raises(support_tag.InvalidTag):
        support_tag.normalize(bad)


@pytest.mark.parametrize(("raw", "clean"),
                         [("a\nb", "a b"), ("a\tb", "a b"), ("a  b", "a b")])
def test_whitespace_is_collapsed_rather_than_refused(raw: str, clean: str) -> None:
    """Перенос строки в теге — вставка из буфера обмена, а не злой умысел.

    Отказ заставил бы человека искать невидимый символ; схлопывание даёт ровно
    тот тег, который он и имел в виду. Проверять запрет на такие символы нечем:
    до неё они не доживают — этим тестом это и зафиксировано.
    """
    assert support_tag.normalize(raw) == clean


def test_titles_reads_the_object_form_of_tags() -> None:
    """`TAGS` приходит объектом, ключ — id тега (docs §9.2)."""
    raw = {"7": {"id": 7, "title": "tgsrc-74-89"}, "9": {"id": 9, "title": TAG}}
    assert support_tag.titles(raw) == ["tgsrc-74-89", TAG]
    assert support_tag.has(raw, TAG)
    assert not support_tag.has(raw, "другой")


def test_empty_tag_marks_nothing() -> None:
    assert not support_tag.has({"7": {"id": 7, "title": ""}}, "")


# ------------------------------------------------------------- дозапись тега
def test_merged_keeps_every_existing_tag() -> None:
    """Главный тест файла.

    `tasks.task.update` с `TAGS` задаёт набор ЦЕЛИКОМ (docs §5.4). Отправить один
    новый тег значит стереть ключ идемпотентности: повтор того же сообщения из
    Telegram создал бы вторую задачу, и И-10 сломался бы молча.
    """
    raw = {"27": {"id": 27, "title": "tgapp-1-6afecad7"}}
    assert support_tag.merged(raw, TAG) == ["tgapp-1-6afecad7", TAG]


def test_merged_returns_none_when_the_tag_is_already_there() -> None:
    """Нечего писать — не пишем: лишний `update` стоит лимита и плодит события."""
    raw = {"27": {"id": 27, "title": TAG}}
    assert support_tag.merged(raw, TAG) is None


def test_merged_does_nothing_for_an_empty_tag() -> None:
    assert support_tag.merged({"27": {"id": 27, "title": "x"}}, "") is None


# ---------------------------------------------------------- пометка при создании
def _draft(**fields: Any) -> Draft:
    return Draft(title="t", description="d", idem_key="tgsrc-74-89",
                 source_message_id=None, fields=fields)


def test_new_task_gets_the_support_tag_next_to_the_idempotency_key() -> None:
    assert _tags(_draft(), TAG) == ["tgsrc-74-89", TAG]


def test_survey_tags_survive_alongside_both() -> None:
    """Теги из ответов опросника не должны вытеснять ни один из наших."""
    assert _tags(_draft(TAGS=["оплата"]), TAG) == ["tgsrc-74-89", "оплата", TAG]


def test_support_tag_is_not_duplicated_if_the_survey_already_added_it() -> None:
    assert _tags(_draft(TAGS=[TAG]), TAG) == ["tgsrc-74-89", TAG]


def test_no_tag_configured_means_no_extra_tag() -> None:
    assert _tags(_draft(), "") == ["tgsrc-74-89"]


# ------------------------------------------------------------------- отчёт
def _entry(entry_id: int, task_id: int, seconds: int) -> timesheet.Entry:
    return timesheet.Entry(entry_id, task_id, seconds,
                           datetime(2026, 8, 15, 12, 0, tzinfo=UTC))


def _tasks() -> dict[int, timesheet.TaskRef]:
    return {
        1: timesheet.TaskRef(1, "из бота", 3, 337, is_support=True),
        2: timesheet.TaskRef(2, "из бота", 5, 337, is_support=True),
        3: timesheet.TaskRef(3, "своя", 3, 337, is_support=False),
    }


def _report(tag: str = TAG) -> timesheet.Report:
    entries = [_entry(1, 1, 3600), _entry(3, 2, 1800), _entry(5, 3, 7200)]
    return timesheet.aggregate(entries, _tasks(), {337: "Сделаны"},
                               year=2026, month=8, tag=tag)


def test_breakdowns_count_only_tagged_tasks() -> None:
    report = _report()
    assert report.total_seconds == 5400        # 3600 + 1800, поддержка
    assert report.all_seconds == 12600         # плюс 7200 своей задачи
    assert report.task_count == 2
    assert report.all_task_count == 3
    assert report.split_by_tag


def test_both_breakdowns_still_add_up_to_the_support_total() -> None:
    """Инвариант отчёта: у задачи один статус и одна стадия.

    Разрезы обязаны сойтись между собой и с суммой поддержки — иначе время
    потерялось по дороге. Со второй суммой они НЕ сходятся, и это правильно.
    """
    report = _report()
    assert sum(b.seconds for b in report.by_status) == report.total_seconds
    assert sum(b.seconds for b in report.by_stage) == report.total_seconds
    assert report.all_seconds > report.total_seconds


def test_without_a_tag_the_report_is_exactly_what_it_was() -> None:
    """Тег не задан — отчёт считает всё и печатает одну сумму, как раньше."""
    report = _report(tag="")
    assert not report.split_by_tag
    assert report.total_seconds == report.all_seconds == 12600
    assert report.task_count == 3


def test_task_outside_the_chat_projects_is_counted_in_neither_sum() -> None:
    """Граница выборки — проекты чата (И-3), и вторая сумма её не расширяет."""
    entries = [_entry(1, 1, 3600), _entry(7, 999, 99999)]
    report = timesheet.aggregate(entries, _tasks(), {337: "Сделаны"},
                                 year=2026, month=8, tag=TAG)
    assert report.total_seconds == 3600
    assert report.all_seconds == 3600


def test_parse_tasks_marks_support_by_tag() -> None:
    raw = {"tasks": [
        {"id": "1", "title": "a", "status": "3", "stageId": "337",
         "tags": {"9": {"id": 9, "title": TAG}}},
        {"id": "2", "title": "b", "status": "3", "stageId": "337", "tags": []},
    ]}
    parsed = timesheet.parse_tasks(raw, TAG)
    assert [t.is_support for t in parsed] == [True, False]


def test_parse_tasks_without_a_tag_marks_nothing() -> None:
    raw = {"tasks": [{"id": "1", "title": "a", "status": "3", "stageId": "0",
                      "tags": {"9": {"id": 9, "title": TAG}}}]}
    assert timesheet.parse_tasks(raw, "")[0].is_support is False


# ----------------------------------------------------------------- отрисовка
def test_rendered_report_shows_both_sums() -> None:
    text = views.render_timesheet(_report(), [PROJECT])
    assert "Поддержка:" in text
    assert "Всего по проектам:" in text
    assert "Из них мимо поддержки:" in text
    assert TAG in text


def test_rendered_report_without_a_tag_says_just_итого() -> None:
    text = views.render_timesheet(_report(tag=""), [PROJECT])
    assert "Итого:" in text
    assert "Всего по проектам" not in text


def test_empty_support_month_still_reports_the_rest() -> None:
    """«Работы не было» и «работу вели мимо бота» — разные новости.

    Пустой отчёт без второй строки читается как первая, а верна вторая: именно
    так выглядит месяц, в котором задачи заводили прямо в Битриксе.
    """
    entries = [_entry(5, 3, 7200)]  # только по задаче без тега
    report = timesheet.aggregate(entries, _tasks(), {337: "Сделаны"},
                                 year=2026, month=8, tag=TAG)
    text = views.render_timesheet(report, [PROJECT])

    assert report.entry_count == 0
    assert report.all_entry_count == 1
    assert "По остальным задачам проектов" in text
    assert "2 ч" in text


def test_hostile_tag_is_escaped_in_the_report() -> None:
    """Тег задаёт человек в приложении, и он уезжает в Telegram (И-6)."""
    entries = [_entry(1, 1, 3600)]
    report = timesheet.aggregate(entries, _tasks(), {337: "Сделаны"},
                                 year=2026, month=8, tag="<b>злой</b>")
    text = views.render_timesheet(report, [PROJECT])
    assert "<b>злой</b>" not in text
    assert "&lt;b&gt;злой&lt;/b&gt;" in text


# ---------------------------------------------------------------- карточка
@dataclass
class _Entries:
    entries: list[Any]
    total_seconds: int
    complete: bool


@dataclass
class _E:
    id: int
    task_id: int
    user_id: int
    seconds: int
    at: Any
    comment: str


def test_timelog_screen_escapes_names_and_comments() -> None:
    """Имя с портала и комментарий — обе подстановки чужие (И-6)."""
    entries = _Entries([_E(1, 233, 7, 3600, None, "разбор <script>")], 3600, True)
    text = views.render_timelog(233, entries, {7: "Иванов <b>"},
                                task_title="Задача & <i>")
    # Проверяются ПОДСТАНОВКИ, а не отсутствие тегов вообще: разметка вокруг них
    # наша, и заголовок экрана обязан остаться жирным.
    for hostile in ("Иванов <b>", "Задача & <i>", "разбор <script>"):
        assert hostile not in text
    assert "&lt;script&gt;" in text
    assert "&lt;b&gt;" in text
    assert "&amp;" in text


def test_timelog_screen_admits_an_incomplete_list() -> None:
    entries = _Entries([_E(1, 233, 7, 3600, None, "")], 36000, False)
    text = views.render_timelog(233, entries, {7: "Иванов"})
    assert "не все списания" in text


# ------------------------------------------------------- живая база: изоляция
ADMIN_URL = os.environ.get("TEST_DATABASE_URL")

live = pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL")


async def _tenant(conn: Any, tag: str) -> int:
    uniq = uuid.uuid4().hex[:8]
    return int(await conn.fetchval(
        "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
        "VALUES ($1, $2, $3, $4, 'active') RETURNING id",
        f"t{uniq}{tag}", f"Теннант {tag}", f"member-{uniq}-{tag}",
        f"t{uniq}{tag}.bitrix24.ru"))


@live
async def test_default_tag_arrives_with_the_tenant(db: Any) -> None:
    """`DEFAULT` колонки и есть обещанное «по умолчанию tg-support».

    Обещание, исполняемое кодом при чтении, а не схемой при записи, врозь с
    схемой живёт ровно до первого запроса мимо этого кода.
    """
    tenant_id = await _tenant(db, "d")
    assert await support_tag.get(tenant_id) == support_tag.DEFAULT_TAG


@live
async def test_tag_of_one_tenant_does_not_leak_into_another(db: Any) -> None:
    """И-2 на самой настройке: у каждого теннанта свой тег и свой отчёт."""
    first, second = await _tenant(db, "a"), await _tenant(db, "b")

    await support_tag.set_tag(first, "support-a")
    assert await support_tag.get(first) == "support-a"
    assert await support_tag.get(second) == support_tag.DEFAULT_TAG

    await support_tag.set_tag(second, "")
    assert await support_tag.get(second) == ""
    assert await support_tag.get(first) == "support-a"


@live
async def test_backfill_mark_is_per_tenant(db: Any) -> None:
    first, second = await _tenant(db, "m"), await _tenant(db, "n")
    assert await support_tag.synced_at(first) is None

    await support_tag.mark_synced(first)
    assert await support_tag.synced_at(first) is not None
    assert await support_tag.synced_at(second) is None
