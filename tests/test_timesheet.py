"""Трудозатраты: два разреза одной суммы.

Главное требование к отчёту — свод по статусам и свод по стадиям обязаны
сходиться между собой и с итогом. У задачи ровно один статус и ровно одна
стадия, поэтому расхождение означало бы потерю времени по дороге, а не
особенность разреза. Проверяется здесь, а не считается очевидным.

Второе, что проверяется, — честность про полноту. Портал отдаёт не больше 50
записей учёта времени и не умеет листать (docs/00-portal-facts.md §5.2). Отчёт,
который в такой ситуации молча покажет сумму, соврёт с уверенным видом.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from b24bot.bot import views
from b24bot.domain import timesheet
from b24bot.domain.timesheet import Entry, TaskRef

MSK = timezone(timedelta(hours=3))
STAGES = {333: "Новые", 335: "Выполняются", 337: "Сделаны"}

TASKS = {
    10: TaskRef(10, "Лид-форма", status=3, stage_id=335),      # выполняется
    11: TaskRef(11, "Почта", status=5, stage_id=337),          # завершена
    12: TaskRef(12, "Домен", status=5, stage_id=337),
    13: TaskRef(13, "Без канбана", status=2, stage_id=0),
}


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=MSK)


ENTRIES = [
    Entry(1, 10, 3600, at(3)),
    Entry(2, 10, 1800, at(4)),
    Entry(3, 11, 7200, at(5)),
    Entry(4, 12, 5400, at(6)),
    Entry(5, 13, 900, at(7)),
    Entry(6, 10, 3600, datetime(2026, 7, 30, 12, tzinfo=MSK)),   # прошлый месяц
    Entry(7, 99, 9999, at(8)),                                   # чужой проект
]


def report(**kw: object) -> timesheet.Report:
    return timesheet.aggregate(ENTRIES, TASKS, STAGES, year=2026, month=8, **kw)  # type: ignore[arg-type]


def test_two_cuts_of_one_sum_agree() -> None:
    """То, ради чего отчёт и делался: суммы разрезов равны итогу."""
    r = report()
    by_status = sum(b.seconds for b in r.by_status)
    by_stage = sum(b.seconds for b in r.by_stage)
    assert by_status == by_stage == r.total_seconds
    # 3600 + 1800 + 7200 + 5400 + 900, без прошлого месяца и чужой задачи
    assert r.total_seconds == 18900


def test_other_month_and_foreign_task_do_not_leak_in() -> None:
    r = report()
    assert r.entry_count == 5, "июльская запись и чужая задача в счёт не идут"
    assert r.task_count == 4


def test_status_cut_names_statuses_of_the_portal() -> None:
    r = report()
    titles = {b.title: b.seconds for b in r.by_status}
    assert titles["Выполняется"] == 5400
    assert titles["Завершена"] == 12600
    assert titles["Ждёт выполнения"] == 900


def test_stage_cut_uses_the_same_names_as_everything_else() -> None:
    """«Вне канбана» — то же слово, что в сводке и карточке, и стоит последним."""
    r = report()
    titles = [b.title for b in r.by_stage]
    assert titles[-1] == views.OUTSIDE_KANBAN
    assert "Выполняются" in titles and "Сделаны" in titles
    unknown = timesheet.aggregate(
        [Entry(1, 20, 60, at(3))], {20: TaskRef(20, "?", 3, 777)}, STAGES,
        year=2026, month=8)
    assert unknown.by_stage[0].title == views.UNKNOWN_STAGE, \
        "незнакомая стадия не должна выглядеть как «вне канбана»"


def test_same_column_of_two_projects_is_one_line() -> None:
    """У каждого проекта свой канбан со своими id, но «Сделаны» — одна колонка.

    В чате с двумя проектами разрез по id дал бы две строки с одинаковым
    названием, и человек читал бы это как ошибку.
    """
    tasks = {1: TaskRef(1, "а", 5, 337), 2: TaskRef(2, "б", 5, 907)}
    titles = {337: "Сделаны", 907: "Сделаны", 335: "Выполняются"}
    r = timesheet.aggregate([Entry(1, 1, 3600, at(3)), Entry(2, 2, 1800, at(3))],
                            tasks, titles, year=2026, month=8)
    assert [b.title for b in r.by_stage] == ["Сделаны"]
    assert r.by_stage[0].seconds == 5400
    assert len(r.by_stage[0].tasks) == 2


def test_stage_order_follows_the_board() -> None:
    """Колонки идут как на доске, служебные строки — после них."""
    tasks = {1: TaskRef(1, "а", 3, 337), 2: TaskRef(2, "б", 3, 333),
             3: TaskRef(3, "в", 3, 0), 4: TaskRef(4, "г", 3, 555)}
    r = timesheet.aggregate([Entry(i, i, 60, at(3)) for i in (1, 2, 3, 4)],
                            tasks, STAGES, year=2026, month=8)
    assert [b.title for b in r.by_stage] == [
        "Новые", "Сделаны", views.OUTSIDE_KANBAN, views.UNKNOWN_STAGE]


def test_one_task_counted_once_in_each_cut() -> None:
    """Две записи по одной задаче — это одна задача, а не две."""
    r = report()
    running = next(b for b in r.by_status if b.title == "Выполняется")
    assert len(running.tasks) == 1


def test_empty_month_is_a_normal_answer() -> None:
    r = timesheet.aggregate(ENTRIES, TASKS, STAGES, year=2026, month=1)
    assert r.total_seconds == 0 and r.entry_count == 0
    assert "списаний времени нет" in views.render_timesheet(r, [])


def test_month_comes_from_the_stamp_itself() -> None:
    """Отметка приходит с поясом портала: приведение к UTC сдвинуло бы декабрь."""
    newyear = Entry(1, 10, 60, datetime(2026, 12, 31, 23, 30, tzinfo=MSK))
    assert timesheet.in_month(newyear, 2026, 12)
    assert not timesheet.in_month(newyear, 2027, 1)


def test_incomplete_data_says_so_in_the_report() -> None:
    """Портал показал не всё — сумма это минимум, и об этом надо сказать."""
    text = views.render_timesheet(report(complete=False, seen=50, total_on_portal=79), [])
    assert "50" in text and "79" in text
    assert "минимум" in text


def test_complete_data_says_nothing_extra() -> None:
    assert "минимум" not in views.render_timesheet(report(), [])


# ------------------------------------------------------------------- разбор
def test_entries_are_parsed_by_work_date_not_entry_date() -> None:
    """`DATE_START` — когда работали, `CREATED_DATE` — когда занесли."""
    parsed = timesheet.parse_entries([{
        "ID": "5", "TASK_ID": "10", "SECONDS": "3600", "MINUTES": "60",
        "DATE_START": "2026-07-01T10:00:00+03:00",
        "CREATED_DATE": "2026-08-15T10:00:00+03:00"}])
    assert parsed[0].at.month == 7


def test_minutes_are_a_fallback_for_seconds() -> None:
    parsed = timesheet.parse_entries([{"ID": "1", "TASK_ID": "2", "MINUTES": "30",
                                       "DATE_START": "2026-08-01T10:00:00+03:00"}])
    assert parsed[0].seconds == 1800


def test_broken_rows_do_not_break_the_report() -> None:
    parsed = timesheet.parse_entries(
        [{"ID": "1"}, {"TASK_ID": "2", "SECONDS": "60"}, "мусор", None])
    assert parsed == []


# ------------------------------------------------------------------- месяцы
def test_month_list_walks_back_over_the_new_year() -> None:
    months = timesheet.months_back(date(2026, 2, 10), count=4)
    assert months == [(2026, 2), (2026, 1), (2025, 12), (2025, 11)]
    assert timesheet.month_title(2025, 12) == "декабрь 2025"


def test_month_token_is_validated_not_trusted() -> None:
    assert timesheet.parse_month("2026-08") == (2026, 8)
    for bad in ("2026-13", "2026-00", "", "август", "26-8-1", "0000-01"):
        assert timesheet.parse_month(bad) is None, bad


# ---------------------------------------------------------------- отрисовка
def test_duration_reads_like_a_person_wrote_it() -> None:
    assert views.fmt_duration(0) == "—"
    assert views.fmt_duration(None) == "—"
    assert views.fmt_duration("18000") == "5 ч"
    assert views.fmt_duration(19800) == "5 ч 30 мин"
    assert views.fmt_duration(1800) == "30 мин"


def test_counts_are_declined() -> None:
    text = views.render_timesheet(report(), [])
    assert "1 задача" in text
    assert "2 задачи" in text
    assert "5 списаний" in text, "не «5 списание» и не «5 списания»"


def test_plural_covers_the_teens_trap() -> None:
    """11–14 считаются как «много», хотя оканчиваются на 1, 2, 3, 4."""
    forms = ("задача", "задачи", "задач")
    assert views.plural(1, *forms) == "1 задача"
    assert views.plural(11, *forms) == "11 задач"
    assert views.plural(21, *forms) == "21 задача"
    assert views.plural(13, *forms) == "13 задач"
    assert views.plural(112, *forms) == "112 задач"
    assert views.plural(0, *forms) == "0 задач"
