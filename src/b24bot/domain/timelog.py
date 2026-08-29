"""Списание времени в задачу: разбор длительности и запись в портал.

Метод — `task.elapseditem.add(TASKID, ARFIELDS)`, проверен на живом портале
29.08.2026 (docs/00-portal-facts.md §5.3). Из него следуют три вещи, которые
видно в коде:

1. **Только `SECONDS`.** `MINUTES` есть в ответе `getlist`, но на входе портал
   его отвергает (`must not contain key "MINUTES"`). Ровно та ловушка «имена на
   входе и на выходе разные», из-за которой поля запроса нельзя брать из ответа.
2. **`COMMENT_TEXT` — не BBCode.** Скобки вернулись дословно, значит `esc_bbcode`
   здесь не нужен и вреден: он заменил бы `[` на полноширинную скобку в тексте,
   который портал и так не разбирает. Обратно в Telegram та же строка уезжает
   через `esc_html` (И-6) — экранирование зависит от места показа, а не от
   происхождения строки.
3. **`DATE_START` уважается, `DATE_STOP` — нет.** Портал ставит своё «сейчас».
   Поэтому «списать за вчера» задаётся началом, и по нему же считается месяц
   в отчёте (`timesheet.in_month`).

Читать чужие списания дорого: у `task.elapseditem.getlist` нет фильтров вовсе, и
он отдаёт максимум 50 записей с каждого конца (§5.2). Поэтому список по задаче —
это срез общего списка, и его полнота **проверяется вычитанием**: сумма видимых
записей задачи против `TIME_SPENT_IN_LOGS` той же задачи. Разошлись — часть
списаний за окном выборки, и об этом надо сказать строкой, а не показывать
неполный список с уверенным видом.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from b24bot.b24 import errors, mapping
from b24bot.b24.client import B24Client
from b24bot.b24.limiter import Lane

log = logging.getLogger(__name__)

AUDIT_ACTION = "task.time.log"
"""Имя в журнале — одно на все двери, константой, а не литералом в каждой.

У остальных действий имена сверяет страж `tests/test_audit_names.py`: их набор
разный у бота и мини-аппа, и списки приходится сравнивать. Здесь действие одно,
и общая константа делает расхождение невозможным, а не находимым."""

MAX_SECONDS = 24 * 3600      # больше суток за один раз — почти всегда опечатка
MIN_SECONDS = 60
COMMENT_MAX = 500

# Кнопки быстрого списания: то, что в поддержке списывают чаще всего.
PRESETS: tuple[int, ...] = (15 * 60, 30 * 60, 3600, 2 * 3600, 4 * 3600, 8 * 3600)

_UNITS = {
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600, "h": 3600,
    "м": 60, "мин": 60, "минут": 60, "минуты": 60, "m": 60, "min": 60,
}
_TOKEN = re.compile(r"(\d+(?:[.,]\d+)?)\s*([а-яёa-z]*)", re.IGNORECASE)


class BadDuration(ValueError):
    """Длительность не разобрана. Текст показывается человеку как есть."""


@dataclass(frozen=True)
class Entry:
    """Одно списание с точки зрения карточки задачи."""

    id: int
    task_id: int
    user_id: int
    seconds: int
    at: datetime | None
    comment: str


def parse_duration(text: str) -> int:
    """«1ч30м», «1:30», «90м», «1.5ч», «90» → секунды.

    Голое число — это МИНУТЫ. Час бы значил, что опечатка в один символ
    («2» вместо «2м») ошибается в шестьдесят раз в сторону завышения, а
    завышенные трудозатраты у клиента дороже заниженных.
    """
    raw = str(text or "").strip().lower().replace(",", ".")
    if not raw:
        raise BadDuration("не указано, сколько времени списать")

    # Форма «1:30» — часы и минуты, как на часах.
    if ":" in raw:
        head, _, tail = raw.partition(":")
        if head.strip().isdigit() and tail.strip().isdigit():
            return checked(int(head) * 3600 + int(tail) * 60)
        raise BadDuration(f"не понял длительность «{text}»")

    if raw.replace(".", "", 1).isdigit():
        return checked(round(float(raw) * 60))

    accumulated = 0.0
    matched = False
    position = 0
    for m in _TOKEN.finditer(raw):
        if raw[position:m.start()].strip():
            raise BadDuration(f"не понял длительность «{text}»")
        position = m.end()
        unit = _UNITS.get(m.group(2))
        if unit is None:
            raise BadDuration(f"не понял длительность «{text}»")
        accumulated += float(m.group(1)) * unit
        matched = True
    if not matched or raw[position:].strip():
        raise BadDuration(f"не понял длительность «{text}»")
    return checked(round(accumulated))


def checked(seconds: int) -> int:
    """Границы разумного. Публичная, потому что мини-апп присылает готовые
    секунды числом и обязан проверять их тем же правилом, что и разбор строки."""
    if seconds < MIN_SECONDS:
        raise BadDuration("минимальное списание — 1 минута")
    if seconds > MAX_SECONDS:
        raise BadDuration("за один раз можно списать не больше 24 часов")
    return seconds


def format_duration(seconds: int) -> str:
    """«1 ч 30 мин» — та же форма, что у `views.fmt_duration`."""
    from b24bot.bot.views import fmt_duration
    return fmt_duration(seconds)


def preset_label(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} м"
    return f"{hours} ч" if hours else f"{minutes} м"


# ------------------------------------------------------------------- запись
async def add(client: B24Client, task_id: int, seconds: int, *,
              comment: str = "", started_at: str | None = None) -> int:
    """Записать списание. Возвращает ID записи учёта времени.

    Идемпотентности здесь нет и быть не может: два одинаковых списания подряд —
    это законный жест («ещё час на ту же задачу»), а не повтор. И-10 говорит про
    мутации, у которых есть естественный ключ; у отрезка времени его нет.
    """
    fields: dict[str, Any] = {"SECONDS": int(seconds)}
    if comment:
        fields["COMMENT_TEXT"] = comment[:COMMENT_MAX]
    if started_at:
        fields["DATE_START"] = started_at
    res = await client.call("task.elapseditem.add",
                            {"TASKID": int(task_id), "ARFIELDS": fields},
                            lane=Lane.INTERACTIVE)
    entry_id = mapping.as_int(res)
    if entry_id is None:
        # Метод отдаёт голый ID. Что угодно другое означает, что портал изменился,
        # и молчать об этом нельзя: списание могло и пройти, и не пройти.
        raise errors.B24Error("UNEXPECTED_RESULT",
                              f"task.elapseditem.add вернул {res!r}, а не ID",
                              "task.elapseditem.add")
    return entry_id


# ------------------------------------------------------------------- чтение
def parse_entries(raw: Any) -> list[Entry]:
    items = raw if isinstance(raw, list) else list((raw or {}).values())
    out: list[Entry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry_id = mapping.as_int(item.get("ID"))
        task_id = mapping.as_int(item.get("TASK_ID"))
        seconds = mapping.as_int(item.get("SECONDS"))
        if entry_id is None or task_id is None or seconds is None:
            continue
        out.append(Entry(entry_id, task_id, mapping.as_int(item.get("USER_ID")) or 0,
                         seconds, _dt(item.get("DATE_START")),
                         str(item.get("COMMENT_TEXT") or "")))
    return out


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


@dataclass
class TaskEntries:
    entries: list[Entry]
    total_seconds: int
    """`TIME_SPENT_IN_LOGS` задачи — истина по сумме, даже когда список неполон."""
    complete: bool
    """Видны ли ВСЕ списания задачи: сумма видимых сошлась с `TIME_SPENT_IN_LOGS`."""


async def for_task(client: B24Client, task_id: int, total_seconds: int) -> TaskEntries:
    """Списания одной задачи из общего списка портала.

    Фильтров у метода нет, страниц тоже (§5.2) — берём оба конца и отбираем своё.
    Полноту доказываем не размером выборки, а сходимостью с суммой самой задачи:
    именно она отличает «списаний больше нет» от «остальные за окном».
    """
    found: dict[int, Entry] = {}
    for direction in ("DESC", "ASC"):
        try:
            raw = await client.call("task.elapseditem.getlist",
                                    {"ORDER": {"ID": direction}}, lane=Lane.INTERACTIVE)
        except errors.B24Error as exc:
            log.warning("список списаний (%s) не прочитан: %s", direction, exc)
            continue
        page = parse_entries(raw)
        for entry in page:
            if entry.task_id == task_id:
                found[entry.id] = entry
        if len(page) < 50:
            break  # меньше страницы — второй конец даст ровно то же самое

    entries = sorted(found.values(), key=lambda e: e.id, reverse=True)
    seen = sum(e.seconds for e in entries)
    return TaskEntries(entries, total_seconds, complete=(seen >= total_seconds))
