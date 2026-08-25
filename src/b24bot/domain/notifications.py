"""Настройки уведомлений: какие события доходят до чата и как часто.

Две независимые ручки, и это разделение намеренное:

* **набор событий** — что вообще считается новостью для этого чата;
* **группировка** — приходить каждой новостью отдельно или одной сводкой
  раз в N минут.

Сводить их в одну ручку («тихий режим») нельзя: чат может хотеть все события и
при этом не хотеть восьми сообщений подряд. Общая ручка выключала бы людям
события, о которых они просили.

**Наследование — инвариант И-9:** `binding` → `project` → `tenant` → системный
дефолт. Отсутствие записи означает «наследовать выше», а не «выключено»; UI
обязан писать явный `false`. В `mclick` обратная трактовка стоила инцидента.

**Окно группировки открывает первое событие.** Пришло событие в чат, где
группировка включена, — назначается время отправки `now() + интервал`, и всё,
что придёт до него, уезжает той же сводкой. Изменение интервала действует со
СЛЕДУЮЩЕГО окна: уже открытое доживает по старому времени. Иначе правка
настройки на середине окна либо задержала бы уже накопленное, либо выплеснула
его в чат немедленно — то самое, от чего группировку и включали.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from b24bot.db.pool import pool

SCOPES = ("tenant", "project", "binding")
# Порядок разрешения. Первый найденный уровень выигрывает.
CHAIN = ("binding", "project", "tenant")


@dataclass(frozen=True)
class Event:
    """Событие, о котором чат может получить уведомление.

    `emitted=False` означает, что кода, который такое уведомление ставит в
    очередь, в системе ещё нет. Такой переключатель в интерфейсе не показывается:
    выключатель, ничего не выключающий, — то же обещание, что команда в меню,
    которой не знает роутер.
    """

    code: str
    label: str
    hint: str
    default: bool
    emitted: bool = True


EVENTS: tuple[Event, ...] = (
    Event("task.created", "Создана задача",
          "По умолчанию выключено: первое событие о незнакомой задаче наполняет "
          "кэш, и при подключении портала иначе хлынет весь накопленный хвост.",
          default=False),
    Event("task.status_changed", "Изменён статус",
          "Взял в работу, вернул в ожидание, отправил на контроль, отложил.",
          default=True),
    Event("task.stage_changed", "Перенесена по канбану",
          "Задача сменила колонку в канбане проекта.", default=True),
    Event("task.responsible_changed", "Сменился ответственный",
          "Задачу передали другому человеку.", default=True),
    Event("task.deadline_changed", "Изменён срок",
          "Срок задачи сдвинули или сняли.", default=True),
    Event("task.completed", "Задача завершена",
          "Отдельно от смены статуса: завершение чаще всего и есть новость.",
          default=True),
    Event("task.deleted", "Задача удалена",
          "Единственное уведомление без кнопок: открывать уже нечего.",
          default=True),
    # Уведомление о комментарии не ставит в очередь ни один обработчик: событие
    # ONTASKCOMMENTADD до нас доходит, но текста комментария в нём нет, а
    # дочитывать его надо из чата задачи (docs/00-portal-facts.md §5.1).
    # Строка стоит здесь, чтобы переключатель не появился раньше самой рассылки.
    Event("task.comment_added", "Новый комментарий",
          "Комментарий к задаче на портале.", default=True, emitted=False),
)

BY_CODE: dict[str, Event] = {e.code: e for e in EVENTS}
DEFAULTS: dict[str, bool] = {e.code: e.default for e in EVENTS}
EMITTED: frozenset[str] = frozenset(e.code for e in EVENTS if e.emitted)

# Допустимые интервалы группировки. Список закрытый: значение приезжает из формы
# в браузере портала, и произвольное число здесь означало бы окно длиной в год.
INTERVALS: tuple[tuple[int, str], ...] = (
    (0, "сразу, каждым сообщением"),
    (5, "раз в 5 минут"),
    (15, "раз в 15 минут"),
    (30, "раз в 30 минут"),
    (60, "раз в час"),
    (180, "раз в 3 часа"),
    (480, "раз в 8 часов"),
)
INTERVAL_MINUTES: frozenset[int] = frozenset(m for m, _ in INTERVALS)
DIGEST_DEFAULT = 0


def interval_label(minutes: int) -> str:
    """Подпись интервала. Неизвестное значение называется числом, а не молчит."""
    for value, label in INTERVALS:
        if value == minutes:
            return label
    return f"раз в {minutes} мин"


# --------------------------------------------------------------- разрешение
@dataclass(frozen=True)
class Resolved:
    """Настройки, применимые к конкретной паре «проект в этом чате»."""

    enabled: dict[str, bool]
    minutes: int
    events_from: str       # binding | project | tenant | default
    minutes_from: str


def resolve_rows(ev_rows: list[tuple[str, str, bool]],
                 dg_rows: list[tuple[str, int]]) -> Resolved:
    """Чистая часть разрешения: строки трёх уровней → применимая настройка.

    Вынесена из запроса намеренно — цепочку наследования проверяют тесты, а не
    живая база: ошибка здесь не падает, а тихо шлёт в чат не то, что просили.
    """
    by_scope: dict[str, dict[str, bool]] = {}
    for kind, code, enabled in ev_rows:
        by_scope.setdefault(kind, {})[code] = enabled

    # Набор событий берётся уровнем ЦЕЛИКОМ, а не по одному коду. Иначе чат,
    # выключивший всё, получил бы новое событие из настройки проекта — то есть
    # «выключил всё» означало бы «всё, кроме того, что добавят потом».
    enabled_map = dict(DEFAULTS)
    events_from = "default"
    for kind in CHAIN:
        if kind in by_scope:
            enabled_map = {**DEFAULTS, **by_scope[kind]}
            events_from = kind
            break

    minutes, minutes_from = DIGEST_DEFAULT, "default"
    by_digest = dict(dg_rows)
    for kind in CHAIN:
        if kind in by_digest:
            minutes, minutes_from = int(by_digest[kind]), kind
            break

    return Resolved(enabled=enabled_map, minutes=minutes,
                    events_from=events_from, minutes_from=minutes_from)


async def resolve(tenant_id: int, project_id: int | None,
                  binding_id: int | None) -> Resolved:
    """Что применяется к этому чату: набор событий и интервал группировки.

    Оба запроса берут разом все три уровня цепочки: спрашивать по уровню за раз
    значило бы три обращения к базе на каждое уведомление в каждом чате.
    """
    async with pool().acquire() as conn:
        ev_rows = await conn.fetch(
            "SELECT scope_kind, code, enabled FROM notification_settings "
            "WHERE tenant_id = $1 AND (scope_kind = 'tenant' "
            "   OR (scope_kind = 'project' AND scope_id = $2) "
            "   OR (scope_kind = 'binding' AND scope_id = $3))",
            tenant_id, project_id or 0, binding_id or 0)
        dg_rows = await conn.fetch(
            "SELECT scope_kind, minutes FROM notification_digest_settings "
            "WHERE tenant_id = $1 AND (scope_kind = 'tenant' "
            "   OR (scope_kind = 'project' AND scope_id = $2) "
            "   OR (scope_kind = 'binding' AND scope_id = $3))",
            tenant_id, project_id or 0, binding_id or 0)
    return resolve_rows(
        [(str(r["scope_kind"]), str(r["code"]), bool(r["enabled"])) for r in ev_rows],
        [(str(r["scope_kind"]), int(r["minutes"])) for r in dg_rows])


# ------------------------------------------------------------------ хранение
async def scope_events(tenant_id: int, scope_kind: str,
                       scope_id: int) -> dict[str, bool] | None:
    """Явная настройка уровня. `None` — записи нет, уровень наследует."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT code, enabled FROM notification_settings "
            "WHERE tenant_id = $1 AND scope_kind = $2 AND scope_id = $3",
            tenant_id, scope_kind, scope_id)
    if not rows:
        return None
    return {str(r["code"]): bool(r["enabled"]) for r in rows}


async def all_scope_events(tenant_id: int,
                           scope_kind: str) -> dict[int, dict[str, bool]]:
    """Явные настройки всех уровней одного вида — экран строится одним запросом."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT scope_id, code, enabled FROM notification_settings "
            "WHERE tenant_id = $1 AND scope_kind = $2", tenant_id, scope_kind)
    out: dict[int, dict[str, bool]] = {}
    for r in rows:
        out.setdefault(int(r["scope_id"]), {})[str(r["code"])] = bool(r["enabled"])
    return out


async def all_scope_minutes(tenant_id: int, scope_kind: str) -> dict[int, int]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT scope_id, minutes FROM notification_digest_settings "
            "WHERE tenant_id = $1 AND scope_kind = $2", tenant_id, scope_kind)
    return {int(r["scope_id"]): int(r["minutes"]) for r in rows}


async def save_events(tenant_id: int, scope_kind: str, scope_id: int,
                      enabled: dict[str, bool] | None) -> None:
    """Записать набор событий уровня. `None` — вернуть уровень к наследованию.

    Пишутся ВСЕ известные коды, в том числе выключенные явным `false`: удалить
    строку выключенного события нельзя, иначе оно вернётся с уровня выше и
    человек получит ровно то, что только что выключил (И-9).
    """
    if scope_kind not in SCOPES:
        raise ValueError(f"неизвестный уровень настройки: {scope_kind}")
    async with pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM notification_settings "
            "WHERE tenant_id = $1 AND scope_kind = $2 AND scope_id = $3",
            tenant_id, scope_kind, scope_id)
        if enabled is None:
            return
        await conn.executemany(
            "INSERT INTO notification_settings (tenant_id, scope_kind, scope_id, "
            "code, enabled) VALUES ($1,$2,$3,$4,$5)",
            [(tenant_id, scope_kind, scope_id, e.code,
              bool(enabled.get(e.code, e.default)))
             for e in EVENTS if e.emitted])


async def save_minutes(tenant_id: int, scope_kind: str, scope_id: int,
                       minutes: int | None) -> None:
    """Интервал группировки уровня. `None` — наследовать выше."""
    if scope_kind not in SCOPES:
        raise ValueError(f"неизвестный уровень настройки: {scope_kind}")
    if minutes is not None and minutes not in INTERVAL_MINUTES:
        raise ValueError(f"интервал вне списка допустимых: {minutes}")
    async with pool().acquire() as conn:
        if minutes is None:
            await conn.execute(
                "DELETE FROM notification_digest_settings "
                "WHERE tenant_id = $1 AND scope_kind = $2 AND scope_id = $3",
                tenant_id, scope_kind, scope_id)
            return
        await conn.execute(
            "INSERT INTO notification_digest_settings (tenant_id, scope_kind, "
            "scope_id, minutes) VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (tenant_id, scope_kind, scope_id) DO UPDATE "
            "SET minutes = EXCLUDED.minutes, updated_at = now()",
            tenant_id, scope_kind, scope_id, minutes)


# -------------------------------------------------------------------- сводка
DIGEST_MAX_LINES = 25
# Предел Telegram — 4096 символов на сообщение, и разметка считается тоже.
# Запас нужен на шапку и на то, чтобы последняя строка не резалась посередине.
DIGEST_MAX_CHARS = 3500


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def span_text(span: timedelta) -> str:
    """Насколько назад уходит самая старая новость в сводке.

    Считается по фактическому возрасту первой строки, а не по настроенному
    интервалу: воркер мог стоять, и «за 15 минут» на сводке за три часа — это
    уверенная неправда о том, что происходит в проекте.
    """
    minutes = max(1, int(span.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}"
    hours, rest = divmod(minutes, 60)
    head = f"{hours} {plural(hours, 'час', 'часа', 'часов')}"
    if not rest:
        return head
    return f"{head} {rest} {plural(rest, 'минуту', 'минуты', 'минут')}"


def digest_messages(lines: list[str], span: timedelta) -> list[str]:
    """Сводка одним сообщением — или несколькими, если одно не вмещает.

    Молчаливое усечение здесь запрещено дважды: общим правилом проекта и смыслом
    самой сводки — она существует, чтобы человек не пропустил новость, а не
    чтобы уместиться в лимит. Поэтому лишнее уходит следующим сообщением, и
    каждое сообщение говорит, какие именно строки в нём и сколько всего.
    """
    if not lines:
        return []
    chunks: list[list[str]] = [[]]
    size = 0
    for line in lines:
        full = len(chunks[-1]) >= DIGEST_MAX_LINES
        if chunks[-1] and (full or size + len(line) > DIGEST_MAX_CHARS):
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += len(line) + 1

    total = len(lines)
    out: list[str] = []
    shown = 0
    for chunk in chunks:
        first, last = shown + 1, shown + len(chunk)
        shown = last
        if len(chunks) == 1:
            head = (f"🔔 <b>Сводка · {total} "
                    f"{plural(total, 'уведомление', 'уведомления', 'уведомлений')} "
                    f"за {span_text(span)}</b>")
        else:
            head = f"🔔 <b>Сводка · {first}–{last} из {total} за {span_text(span)}</b>"
        out.append(head + "\n\n" + "\n".join(chunk))
    return out
