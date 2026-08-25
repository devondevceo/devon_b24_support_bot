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

    `scopes` — уровни, на которых настройка действительно читается. У напоминаний
    в личку адресата нет чата вовсе: `reminders` спрашивает их по проекту, и
    переключатель уровня чата остался бы таким же мёртвым выключателем.

    `kind` — «task» для новостей о задачах (у каждой есть набор кнопок под
    уведомлением) и «proactive» для сообщений по расписанию: их шлёт не событие
    портала, а `domain/reminders.py`.
    """

    code: str
    label: str
    hint: str
    default: bool
    emitted: bool = True
    kind: str = "task"
    scopes: tuple[str, ...] = SCOPES


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

    # Проактивные сообщения (`domain/reminders.py`): их шлёт не событие портала,
    # а расписание. Настройки те же самые и в той же таблице, поэтому и экран
    # общий: человек ищет «где выключить, чтобы не писало» в одном месте, а не
    # по признаку, который знаем только мы.
    Event("reminder.approval", "Напоминание о подтверждении",
          "Задача ждёт решения ответственного: личное напоминание через четыре "
          "часа и разговор в чате проекта через сутки.",
          default=True, kind="proactive", scopes=("tenant", "project")),
    Event("reminder.deadline", "Напоминание о сроке",
          "Личное сообщение ответственному за два часа до срока задачи.",
          default=True, kind="proactive", scopes=("tenant", "project")),
    Event("digest.daily", "Утренняя сводка в чат",
          "Раз в сутки числами: просрочено, срок сегодня, ждут подтверждения, "
          "без движения. Тот же переключатель, что команда «/digest» в чате.",
          default=False, kind="proactive"),
)

BY_CODE: dict[str, Event] = {e.code: e for e in EVENTS}
DEFAULTS: dict[str, bool] = {e.code: e.default for e in EVENTS}
EMITTED: frozenset[str] = frozenset(e.code for e in EVENTS if e.emitted)
TASK_DEFAULTS: dict[str, bool] = {e.code: e.default for e in EVENTS if e.kind == "task"}
PROACTIVE_DEFAULTS: dict[str, bool] = {e.code: e.default for e in EVENTS
                                       if e.kind == "proactive"}


def settable(scope_kind: str) -> tuple[Event, ...]:
    """Переключатели, которые на этом уровне действительно что-то решают."""
    return tuple(e for e in EVENTS if e.emitted and scope_kind in e.scopes)

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
    minutes_from: str      # binding | project | tenant | default


def resolve_rows(ev_rows: list[tuple[str, str, bool]],
                 dg_rows: list[tuple[str, int]]) -> Resolved:
    """Чистая часть разрешения: строки трёх уровней → применимая настройка.

    Вынесена из запроса намеренно — цепочку наследования проверяют тесты, а не
    живая база: ошибка здесь не падает, а тихо шлёт в чат не то, что просили.

    **Разрешение покодовое, а не уровнем целиком.** Первое желание было отдать
    уровень целиком: «чат выключил всё» тогда означало бы «и то, что добавят
    потом». Но в эту таблицу пишет не только экран настройки: `/digest` в чате
    ставит ОДНУ строку `digest.daily` на уровне привязки (`reminders.set_digest`).
    При правиле «уровень целиком» одна такая строка отменяла бы для этого чата
    всю настройку проекта разом — молча и в сторону, о которой никто не просил.
    Два писателя в одну таблицу обязаны читать её одинаково, и покодовое
    разрешение — то, что записано в docs/20-data-model.md §9.
    """
    by_scope: dict[str, dict[str, bool]] = {}
    for kind, code, enabled in ev_rows:
        by_scope.setdefault(kind, {})[code] = enabled

    enabled_map = dict(DEFAULTS)
    for code in DEFAULTS:
        for kind in CHAIN:
            if code in by_scope.get(kind, {}):
                enabled_map[code] = by_scope[kind][code]
                break

    minutes, minutes_from = DIGEST_DEFAULT, "default"
    by_digest = dict(dg_rows)
    for kind in CHAIN:
        if kind in by_digest:
            minutes, minutes_from = int(by_digest[kind]), kind
            break

    return Resolved(enabled=enabled_map, minutes=minutes, minutes_from=minutes_from)


async def enabled_one(tenant_id: int, project_id: int | None, code: str, *,
                      binding_id: int | None = None) -> bool:
    """Разрешение одного кода одним запросом.

    Живёт рядом с `resolve`, а не вместо неё: доставка новости о задаче
    спрашивает разом всё, что применимо к чату, а напоминания ходят по проектам
    поодиночке, и полное разрешение на каждый проект было бы лишней парой
    запросов. Цепочка при этом одна и та же — здесь, а не в двух модулях.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT scope_kind, enabled FROM notification_settings "
            "WHERE tenant_id = $1 AND code = $2 "
            "AND ((scope_kind = 'binding' AND scope_id = $4) "
            "  OR (scope_kind = 'project' AND scope_id = $3) OR scope_kind = 'tenant')",
            tenant_id, code, project_id or 0, binding_id or 0)
    by_scope = {str(r["scope_kind"]): bool(r["enabled"]) for r in rows}
    for kind in CHAIN:
        if kind in by_scope:
            return by_scope[kind]
    return DEFAULTS.get(code, False)


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
    return (await all_scope_events(tenant_id, scope_kind)).get(scope_id)


async def all_scope_events(tenant_id: int,
                           scope_kind: str) -> dict[int, dict[str, bool]]:
    """Явные настройки всех уровней одного вида — экран строится одним запросом.

    Коды, которые на этом уровне не настраиваются, отбрасываются: строка чужого
    или устаревшего кода не должна превращать уровень в «настроенный» на вид.
    """
    codes = {e.code for e in settable(scope_kind)}
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT scope_id, code, enabled FROM notification_settings "
            "WHERE tenant_id = $1 AND scope_kind = $2", tenant_id, scope_kind)
    out: dict[int, dict[str, bool]] = {}
    for r in rows:
        if str(r["code"]) in codes:
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

    Пишутся ВСЕ коды, настраиваемые на этом уровне, в том числе выключенные явным
    `false`: удалить строку выключенного события нельзя, иначе оно вернётся с
    уровня выше и человек получит ровно то, что только что выключил (И-9).

    Трогаются РОВНО те коды, которые уровень настраивает, — отсюда `code = ANY`
    в удалении. Снести здесь всё по уровню значило бы вместе с показанными
    переключателями стереть чужие строки в той же таблице: `digest.daily`
    ставится ещё и командой `/digest` из чата, а завтра появится третий писатель.
    """
    if scope_kind not in SCOPES:
        raise ValueError(f"неизвестный уровень настройки: {scope_kind}")
    codes = [e.code for e in settable(scope_kind)]
    async with pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM notification_settings "
            "WHERE tenant_id = $1 AND scope_kind = $2 AND scope_id = $3 "
            "AND code = ANY($4::text[])",
            tenant_id, scope_kind, scope_id, codes)
        if enabled is None:
            return
        await conn.executemany(
            "INSERT INTO notification_settings (tenant_id, scope_kind, scope_id, "
            "code, enabled) VALUES ($1,$2,$3,$4,$5)",
            [(tenant_id, scope_kind, scope_id, e.code,
              bool(enabled.get(e.code, e.default)))
             for e in settable(scope_kind)])


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
