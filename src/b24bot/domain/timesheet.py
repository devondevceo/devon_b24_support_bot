"""Трудозатраты за месяц: свод по статусам и по стадиям.

Портал ограничивает нас сильнее, чем хотелось бы (docs/00-portal-facts.md §5.2):
`task.elapseditem.getlist` отдаёт **не больше 50 записей**, `start` игнорирует,
`NAV_PARAMS` отвечает пустотой, а фильтры роняет `ERROR_CORE`. Работает только
`ORDER`. Поэтому список берётся с двух концов — 50 самых свежих и 50 самых
старых, — объединяется по `ID`, и размер объединения сверяется с `total` из
конверта. Совпало — данные полные; не совпало — отчёт обязан сказать об этом
строкой, а не показывать неполную сумму с уверенным видом.

Два разреза одной и той же суммы. У задачи ровно один статус и ровно одна
стадия, поэтому сумма по статусам и сумма по стадиям обязаны совпадать между
собой и с итогом — это проверяется тестом, а не считается очевидным. Расхождение
означало бы, что часть времени потерялась по дороге.

Границей выборки, как везде, служат группы проектов чата (И-3): записи по чужим
задачам не попадают ни в один разрез и не считаются в итог — область отчёта
названа в его шапке.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from b24bot.b24 import errors, mapping
from b24bot.b24.client import B24Client
from b24bot.b24.limiter import Lane

log = logging.getLogger(__name__)

PAGE = 50            # столько отдаёт портал и столько же ждём в списке задач
MAX_TASK_PAGES = 20  # 1000 задач: дальше отчёт по чату всё равно теряет смысл
CACHE_TTL = timedelta(minutes=5)
MONTHS_OFFERED = 6

MONTH_NAMES = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
               "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


@dataclass(frozen=True)
class Entry:
    """Одно списание времени."""

    id: int
    task_id: int
    seconds: int
    at: datetime
    """`DATE_START` — когда работали. `CREATED_DATE` это когда занесли, и при
    списании задним числом они расходятся."""


@dataclass(frozen=True)
class TaskRef:
    id: int
    title: str
    status: int
    stage_id: int


@dataclass
class Bucket:
    title: str
    seconds: int = 0
    tasks: set[int] = field(default_factory=set)


@dataclass
class Report:
    year: int
    month: int
    by_status: list[Bucket]
    by_stage: list[Bucket]
    total_seconds: int
    task_count: int
    entry_count: int
    complete: bool
    """Видели ли мы все записи портала. False — сумма снизу, а не точная."""
    seen: int = 0
    total_on_portal: int = 0

    @property
    def title(self) -> str:
        return f"{MONTH_NAMES[self.month - 1]} {self.year}"


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------- разбор
def parse_entries(raw: Any) -> list[Entry]:
    """Записи учёта времени из ответа портала. Мусор пропускаем молча — он там
    не наш, а сумма важнее одной кривой строки."""
    out: list[Entry] = []
    items = raw if isinstance(raw, list) else list((raw or {}).values())
    for item in items:
        if not isinstance(item, dict):
            continue
        task_id = mapping.as_int(item.get("TASK_ID"))
        seconds = mapping.as_int(item.get("SECONDS"))
        if seconds is None:
            minutes = mapping.as_int(item.get("MINUTES"))
            seconds = minutes * 60 if minutes is not None else None
        when = _dt(item.get("DATE_START")) or _dt(item.get("CREATED_DATE"))
        entry_id = mapping.as_int(item.get("ID"))
        if task_id is None or seconds is None or when is None or entry_id is None:
            continue
        out.append(Entry(entry_id, task_id, seconds, when))
    return out


def parse_tasks(raw: Any) -> list[TaskRef]:
    tasks = raw.get("tasks", []) if isinstance(raw, dict) else raw
    out: list[TaskRef] = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        task_id = mapping.as_int(t.get("id"))
        if task_id is None:
            continue
        out.append(TaskRef(task_id, str(t.get("title") or ""),
                           mapping.as_int(t.get("status")) or 0,
                           mapping.as_int(t.get("stageId")) or 0))
    return out


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# --------------------------------------------------------------- агрегация
def in_month(entry: Entry, year: int, month: int) -> bool:
    """Месяц берём из самой отметки времени.

    Она приходит с часовым поясом портала, поэтому её собственные год и месяц —
    это и есть «месяц по календарю портала». Приводить к UTC нельзя: у декабря
    31-го числа это сдвинуло бы часть записей в другой год.
    """
    return entry.at.year == year and entry.at.month == month


def aggregate(entries: list[Entry], tasks: dict[int, TaskRef],
              stage_titles: dict[int, str], *, year: int, month: int,
              complete: bool = True, seen: int = 0,
              total_on_portal: int = 0) -> Report:
    """Свод за месяц. Записи по задачам вне `tasks` не считаются вовсе."""
    from b24bot.bot.views import stage_label

    by_status: dict[int, Bucket] = {}
    # Стадии сводятся по НАЗВАНИЮ, а не по идентификатору: у каждого проекта свой
    # канбан со своими id, и в чате с двумя проектами «Сделаны» иначе появились бы
    # двумя строками. Для человека это одна колонка, и складывать их надо вместе.
    by_stage: dict[str, Bucket] = {}
    total = 0
    counted: set[int] = set()
    entry_count = 0

    for e in entries:
        if not in_month(e, year, month):
            continue
        task = tasks.get(e.task_id)
        if task is None:
            continue  # чужой проект: область отчёта названа в шапке
        status = by_status.setdefault(
            task.status,
            Bucket(mapping.STATUS_TITLES.get(task.status, f"статус {task.status}")))
        label = stage_label(task.stage_id, stage_titles)
        stage = by_stage.setdefault(label, Bucket(label))
        for bucket in (status, stage):
            bucket.seconds += e.seconds
            bucket.tasks.add(task.id)
        total += e.seconds
        counted.add(task.id)
        entry_count += 1

    return Report(
        year=year, month=month,
        by_status=[b for _, b in sorted(by_status.items())],
        by_stage=_ordered_stages(by_stage, stage_titles),
        total_seconds=total, task_count=len(counted), entry_count=entry_count,
        complete=complete, seen=seen, total_on_portal=total_on_portal)


def _ordered_stages(buckets: dict[str, Bucket], stage_titles: dict[int, str]
                    ) -> list[Bucket]:
    """Порядок — как колонки стоят в канбане; служебные строки в конце.

    «Вне канбана» и «Стадия не опознана» — не колонки, а состояния, и место им
    после настоящих колонок, иначе они разрывают привычный порядок доски.
    """
    from b24bot.bot.views import OUTSIDE_KANBAN, UNKNOWN_STAGE

    order: dict[str, int] = {}
    for i, title in enumerate(stage_titles.values()):
        order.setdefault(title, i)
    special = {OUTSIDE_KANBAN: 1, UNKNOWN_STAGE: 2}
    return [b for _, b in sorted(
        buckets.items(),
        key=lambda kv: (special.get(kv[0], 0), order.get(kv[0], 10**6), kv[0]))]


def months_back(today: date, count: int = MONTHS_OFFERED) -> list[tuple[int, int]]:
    """Последние месяцы, начиная с текущего: [(2026, 8), (2026, 7), …]."""
    out: list[tuple[int, int]] = []
    year, month = today.year, today.month
    for _ in range(count):
        out.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


def month_title(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {year}"


def parse_month(value: str) -> tuple[int, int] | None:
    """«2026-08» из полезной нагрузки кнопки."""
    head, _, tail = str(value).partition("-")
    if not (head.isdigit() and tail.isdigit()):
        return None
    year, month = int(head), int(tail)
    return (year, month) if 2000 <= year <= 2999 and 1 <= month <= 12 else None


# ------------------------------------------------------------------ портал
@dataclass
class Snapshot:
    tasks: dict[int, TaskRef]
    entries: list[Entry]
    complete: bool
    seen: int
    total_on_portal: int
    at: datetime


_cache: dict[tuple[int, tuple[int, ...]], Snapshot] = {}


async def snapshot(client: B24Client, tenant_id: int, group_ids: list[int], *,
                   force: bool = False) -> Snapshot:
    """Задачи и записи времени одним снимком, с коротким кэшем.

    Кэш нужен не ради экономии вообще, а ради месяцев: человек листает их
    кнопками подряд, и каждый месяц — это срез одного и того же снимка.
    """
    key = (tenant_id, tuple(sorted(group_ids)))
    hit = _cache.get(key)
    if hit is not None and not force and _now() - hit.at < CACHE_TTL:
        return hit

    tasks = await _fetch_tasks(client, group_ids)
    entries, seen, total = await _fetch_entries(client)
    snap = Snapshot(tasks=tasks, entries=entries,
                    complete=(total == 0 or seen >= total),
                    seen=seen, total_on_portal=total, at=_now())
    _cache[key] = snap
    return snap


async def _fetch_tasks(client: B24Client, group_ids: list[int]) -> dict[int, TaskRef]:
    """ВСЕ задачи проектов, включая закрытые: время списывают и на них.

    Постраничность здесь честная (метод современный), но защита от повтора всё
    равно стоит: у соседнего метода `start` молча игнорируется, и одинаковая
    первая страница крутилась бы вечно.
    """
    if not group_ids:
        return {}
    out: dict[int, TaskRef] = {}
    previous_first: int | None = None
    for page in range(MAX_TASK_PAGES):
        res = await client.call("tasks.task.list", {
            "filter": {"GROUP_ID": group_ids},
            "select": ["ID", "TITLE", "STATUS", "STAGE_ID", "GROUP_ID"],
            "order": {"ID": "asc"},
            "start": page * PAGE,
        }, lane=Lane.INTERACTIVE)
        chunk = parse_tasks(res)
        if not chunk or (previous_first is not None and chunk[0].id == previous_first):
            break
        previous_first = chunk[0].id
        out.update({t.id: t for t in chunk})
        if len(chunk) < PAGE:
            break
    return out


async def _fetch_entries(client: B24Client) -> tuple[list[Entry], int, int]:
    """Записи с двух концов списка: свежие и старые.

    Больше 50 за раз метод не отдаёт ни одним способом, поэтому единственный
    доступный рычаг — направление сортировки (docs/00-portal-facts.md §5.2).
    """
    found: dict[int, Entry] = {}
    for direction in ("DESC", "ASC"):
        try:
            raw = await client.call("task.elapseditem.getlist",
                                    {"ORDER": {"ID": direction}},
                                    lane=Lane.INTERACTIVE)
        except errors.B24Error as exc:
            log.warning("учёт времени (%s) не прочитан: %s", direction, exc)
            continue
        for entry in parse_entries(raw):
            found[entry.id] = entry
        if len(parse_entries(raw)) < PAGE:
            # Записей меньше страницы — второй конец даст ровно то же самое.
            break

    total = await _total_entries(client)
    return list(found.values()), len(found), total


async def _total_entries(client: B24Client) -> int:
    """`total` живёт в конверте ответа, а `call` отдаёт только `result`.

    Знать его обязательно: без него нельзя отличить «показали всё» от
    «показали первые 50 из трёхсот», а разница между ними — это разница между
    отчётом и красивым враньём.
    """
    try:
        return await client.call_total("task.elapseditem.getlist", {})
    except errors.B24Error as exc:
        log.warning("не удалось узнать общее число записей учёта времени: %s", exc)
        return 0
