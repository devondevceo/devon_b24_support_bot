"""Проактивные сообщения: напоминания, эскалации и утренняя сводка.

До сих пор бот говорил только в ответ — на команду, на нажатие, на событие
портала. Здесь он впервые начинает говорить сам, и от этого зависят три вещи,
каждая из которых раньше молчала.

**Подтверждение задачи.** Запрос уходит в личку ответственному, а личка бывает
недоступна: пока человек сам не написал боту, Telegram не даёт написать ему
первым. Раньше на этом всё и кончалось — задача висела `pending`, и об этом не
знал никто. Теперь недоставленный запрос через полчаса уходит в чат проекта
словами, а доставленный, но забытый — напоминанием через четыре часа и
эскалацией в чат через сутки.

**Срок задачи.** Наступал молча. Теперь за два часа до срока ответственный
получает личное сообщение — если он сопоставлен с Битриксом и хоть раз открывал
диалог с ботом.

**Утренняя сводка.** Раз в сутки в чат уходят числа: просрочено, срок сегодня,
ждут подтверждения, без движения. По умолчанию выключена и включается командой
`/digest` — сообщение, которое никто не звал, в чужом рабочем чате должно
появляться только по осознанному решению.

Четыре решения, которые стоит понимать до чтения кода:

1. **Отметка «отправлено» ставится ДО отправки** (`reminder_marks`, миграция
   `0015`). Условие напоминания истинно всё окно целиком, поэтому без отметки
   каждый проход слал бы его заново. Направление выбрано осознанно: цена отметки
   до — одно потерянное напоминание; цена отметки после — поток одинаковых
   сообщений человеку, пока Telegram отвечает ошибкой.
2. **Сводка содержит только числа, без заголовков задач.** Она собрана сервисным
   токеном, то есть видит всё, что видит установщик приложения, а не читатель
   чата. Подробности открываются кнопкой — а кнопка уже работает под личным
   токеном нажавшего, и Битрикс режет права сам (`_open_summary`). Ровно поэтому
   `/digest`, набранный человеком, считает то же самое его собственным токеном:
   там ограничения читателя уже учтены, и числа могут честно отличаться от
   ночной рассылки.
3. **Сводка молчит, когда сообщать нечего.** Ежедневное «всё по сроку» в рабочем
   чате — это шум, который перестают читать вместе со всем остальным. Отметка
   при этом всё равно ставится: вопрос на сегодня решён.
4. **Проходы идут по расписанию в памяти процесса** (`run_due`), как `_failed_until`
   в `domain/sync.py`. Перезапуск воркера — законный повод проверить всё заново:
   от этого никто не получит второго сообщения, за это отвечают отметки в базе.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from b24bot.b24 import errors
from b24bot.b24.mapping import as_int
from b24bot.b24.tokens import NeedsReauth
from b24bot.bot import keyboards, views
from b24bot.core.text import esc_html
from b24bot.db.pool import pool
from b24bot.domain import access, approvals, dm, events
from b24bot.domain.context import issue_token

log = logging.getLogger(__name__)

# Коды настроек. Разрешение читается по цепочке привязка → проект → теннант →
# дефолт (`events.is_enabled`, инвариант И-9), дефолты объявлены в `events.DEFAULTS`.
CODE_APPROVAL = "reminder.approval"
CODE_DEADLINE = "reminder.deadline"
CODE_DIGEST = "digest.daily"

# Сколько ждать, прежде чем напомнить о неподтверждённой задаче лично и прежде
# чем сказать о ней в чате. Четыре часа — примерно полрабочего дня: раньше это
# уже дёрганье, позже задача успевает пролежать смену.
APPROVAL_REMIND_AFTER = timedelta(hours=4)
APPROVAL_ESCALATE_AFTER = timedelta(hours=24)
# Недоставленный запрос ждать дольше незачем: он не дошёл вовсе, и время его не
# приблизит. Полчаса — на случай, если человек как раз сейчас открывает бота.
APPROVAL_UNDELIVERED_AFTER = timedelta(minutes=30)

# За сколько предупреждать о сроке. Два часа — столько, чтобы успеть сделать или
# честно перенести, и не столько, чтобы забыть о напоминании к моменту срока.
DEADLINE_WINDOW = timedelta(hours=2)

# Час утренней сводки по местному времени портала и ширина окна. Окно нужно на
# случай, когда воркер в девять утра лежал: сводка уйдёт при первой возможности,
# но не в одиннадцать вечера.
DIGEST_HOUR = 9
DIGEST_WINDOW = timedelta(hours=3)
DEFAULT_TZ = "Europe/Moscow"
# «Без движения» — задача, которую никто не трогал неделю. Не ошибка сама по
# себе, но в сводке поддержки это и есть то, о чём забыли.
STALE_AFTER = timedelta(days=7)

# Периоды проходов. Подтверждения проверяются чаще: там счёт идёт на минуты в
# сценарии недоставки. Сроки и сводка — величины часовые.
APPROVAL_PASS = timedelta(minutes=5)
DEADLINE_PASS = timedelta(minutes=15)
DIGEST_PASS = timedelta(minutes=15)

APPROVALS_PER_PASS = 50
# Отметки живут дольше любого окна, но не вечно. Воскресить напоминание удалением
# отметки нельзя: срок к тому времени давно в прошлом, а отпечаток сводки — дата.
MARK_RETENTION = timedelta(days=60)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_next_run: dict[str, datetime] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def tz_of(name: str | None) -> tzinfo:
    """Часовой пояс портала. Неизвестное имя — не повод падать в UTC молча.

    UTC сдвинул бы «утро» на три часа и превратил бы «срок сегодня» в неправду
    ровно в те часы, когда это важнее всего. Запасной вариант — тот же, что стоит
    умолчанием у колонки `tenants.tz`.
    """
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (KeyError, ValueError):
        log.warning("неизвестный часовой пояс %r, считаю по +03:00", name)
        return timezone(timedelta(hours=3))


def fmt_age(age: timedelta) -> str:
    hours = int(age.total_seconds() // 3600)
    if hours < 48:
        return f"{max(hours, 1)} ч"
    return f"{hours // 24} дн"


# ------------------------------------------------------------------- отметки
async def mark_once(tenant_id: int, scope: str, scope_id: int, kind: str,
                    fingerprint: str = "") -> bool:
    """`True` — этот повод отмечается впервые, значит сообщение надо отправить.

    Повторный вызов с тем же отпечатком даёт `False`. Сменившийся отпечаток —
    новый повод: перенесённый срок задачи, следующий день у сводки.

    Гонки двух воркеров здесь нет по построению: `INSERT ... ON CONFLICT DO UPDATE`
    атомарен, и строку получит ровно один вызов.
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO reminder_marks (tenant_id, scope, scope_id, kind, fingerprint)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (tenant_id, scope, scope_id, kind) DO UPDATE
               SET fingerprint = EXCLUDED.fingerprint, sent_at = now()
             WHERE reminder_marks.fingerprint <> EXCLUDED.fingerprint
            RETURNING sent_at
            """, tenant_id, scope, scope_id, kind, fingerprint)
    return row is not None


async def clear_mark(tenant_id: int, scope: str, scope_id: int, kind: str) -> None:
    """Снять отметку — «повод остаётся открытым».

    Нужно ровно там, где отметка ставится ДО работы, а работа не состоялась не по
    вине повода: портал не ответил на девятиутренний запрос. Без снятия сводка
    пропала бы на весь день, и молча — вопрос «за сегодня» считался бы решённым.
    """
    async with pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM reminder_marks WHERE tenant_id = $1 AND scope = $2 "
            "AND scope_id = $3 AND kind = $4", tenant_id, scope, scope_id, kind)


async def cleanup_marks() -> int:
    async with pool().acquire() as conn:
        gone = await conn.fetchval(
            "WITH old AS (DELETE FROM reminder_marks WHERE sent_at < $1 RETURNING 1) "
            "SELECT count(*) FROM old", _now() - MARK_RETENTION)
    return int(gone or 0)


# ------------------------------------------------------- подтверждение задачи
async def approvals_pass(limit: int = APPROVALS_PER_PASS) -> int:
    """Напоминания и эскалации по неподтверждённым задачам.

    Строки, уже дошедшие до эскалации, из выборки исключены: дальше по этой
    цепочке идти некуда, и держать их в очереди значило бы просматривать одни и
    те же самые старые запросы каждым проходом.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.tenant_id, a.project_id, a.b24_task_id, a.task_title,
                   a.requested_at, a.notified_at, a.responsible_user_id,
                   u.tg_user_id AS responsible_tg_id, u.display_name,
                   m.b24_user_id, t.b24_domain
              FROM task_approvals a
              JOIN tenants t ON t.id = a.tenant_id AND t.status = 'active'
              JOIN users u ON u.id = a.responsible_user_id
              LEFT JOIN tenant_members m ON m.tenant_id = a.tenant_id
                                        AND m.user_id = a.responsible_user_id
             WHERE a.status = 'pending'
               AND NOT EXISTS (SELECT 1 FROM reminder_marks r
                                WHERE r.tenant_id = a.tenant_id AND r.scope = 'approval'
                                  AND r.scope_id = a.id AND r.kind = 'escalate')
             ORDER BY a.requested_at
             LIMIT $1
            """, limit)

    sent = 0
    for row in rows:
        try:
            sent += await _approval_step(row)
        except Exception:
            log.exception("напоминание по запросу подтверждения %s не отправлено",
                          row["id"])
    return sent


async def _approval_step(row: Any) -> int:
    """Один шаг цепочки для одного запроса. За проход — не больше одного сообщения.

    Шаги намеренно не складываются: воркер, пролежавший сутки, иначе прислал бы
    человеку напоминание и следом эскалацию в чат одной пачкой — то есть шум
    ровно там, где мы боремся с шумом.
    """
    tenant_id, approval_id = int(row["tenant_id"]), int(row["id"])
    if not await events.is_enabled(tenant_id, int(row["project_id"]), CODE_APPROVAL):
        return 0
    age = _now() - row["requested_at"]

    if row["notified_at"] is None:
        # Личка могла открыться с прошлого раза: человек написал боту сам.
        if await approvals.deliver(tenant_id, approval_id):
            return 1
        if age >= APPROVAL_UNDELIVERED_AFTER and await mark_once(
                tenant_id, "approval", approval_id, "escalate"):
            await _escalate(row, age, undelivered=True)
            return 1
        return 0

    if age >= APPROVAL_REMIND_AFTER and await mark_once(
            tenant_id, "approval", approval_id, "remind"):
        await approvals.deliver(tenant_id, approval_id,
                                heading=approvals.HEADING_REMINDER)
        return 1
    if age >= APPROVAL_ESCALATE_AFTER and await mark_once(
            tenant_id, "approval", approval_id, "escalate"):
        await _escalate(row, age, undelivered=False)
        return 1
    return 0


def escalation_text(*, ref: str, title: str, who: str, age: timedelta,
                    undelivered: bool, has_buttons: bool) -> str:
    """Текст эскалации в чат. Вынесен отдельно, потому что это обещание человеку:
    сказать, что именно сломалось и что с этим делать."""
    head = ("🙋 <b>Некому подтвердить задачу</b>" if undelivered
            else "🙋 <b>Задача ждёт подтверждения</b>")
    lines = [head, "", f"{ref} · {esc_html(title)}", f"Решение за: {esc_html(who)}"]
    if undelivered:
        lines += [
            "",
            "Запрос не удалось отправить в личку: пока человек сам не начал диалог "
            "с ботом, написать ему первым нельзя.",
        ]
        lines.append("Кнопки ниже работают и здесь — нажать их может только он."
                     if has_buttons
                     else "У ответственного не привязан Telegram — некому и нажать. "
                          "Назначьте другого во вкладке «Подтверждение».")
    else:
        lines[-1] += f" · без ответа {fmt_age(age)}"
    return "\n".join(lines)


async def _escalate(row: Any, age: timedelta, *, undelivered: bool) -> None:
    """Сказать о зависшем подтверждении в чатах проекта.

    Кнопки те же, что в личке, и владелец у них тот же — ответственный. Нажатие
    инлайн-кнопки в группе не требует диалога с ботом, поэтому именно они и
    закрывают сценарий недоставки: решение можно принять, ни разу не открыв личку.
    """
    tenant_id, approval_id = int(row["tenant_id"]), int(row["id"])
    task_id = int(row["b24_task_id"])
    responsible_tg = as_int(row["responsible_tg_id"])
    ref = views.task_ref(task_id, domain=str(row["b24_domain"] or ""),
                         b24_user_id=row["b24_user_id"])
    text = escalation_text(ref=ref, title=str(row["task_title"] or ""),
                           who=str(row["display_name"] or "ответственный"),
                           age=age, undelivered=undelivered,
                           has_buttons=responsible_tg is not None)

    targets = await events.chat_targets(tenant_id, int(row["project_id"]))
    for t in targets:
        if t["bot_ref"] is None:
            continue
        markup = None
        if responsible_tg is not None:
            confirm = await issue_token(
                "task_approval", tenant_id=tenant_id, owner_tg_id=responsible_tg,
                payload={"approval_id": approval_id, "decision": "confirm"},
                ttl=timedelta(days=30))
            reject = await issue_token(
                "task_approval", tenant_id=tenant_id, owner_tg_id=responsible_tg,
                payload={"approval_id": approval_id, "decision": "reject"},
                ttl=timedelta(days=30))
            markup = keyboards.inline([[
                keyboards.cb("av", confirm, "✅ Подтвердить"),
                keyboards.cb("av", reject, "❌ Отклонить"),
            ]])
        await events.enqueue(
            tenant_id, bot_ref=int(t["bot_ref"]), chat_ref=int(t["chat_ref"]),
            thread_id=t["thread_id"], kind="approval.escalated", text=text,
            markup=markup, dedup_key=f"approval:{approval_id}:{t['chat_ref']}")
    log.info("запрос подтверждения %s эскалирован в %d чат(ов), недоставка=%s",
             approval_id, len(targets), undelivered)


# --------------------------------------------------------------- сроки задач
@dataclass(frozen=True)
class Served:
    """Проект, который бот действительно обслуживает: он привязан к живому чату."""

    id: int
    b24_group_id: int
    name: str
    client_name: str


async def served_projects(tenant_id: int) -> list[Served]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT p.id, p.b24_group_id, p.name, cl.name AS client_name
              FROM projects p
              JOIN clients cl ON cl.id = p.client_id
              JOIN chat_bindings b ON b.project_id = p.id AND b.status = 'active'
              JOIN tg_chats c ON c.id = b.chat_ref AND c.status IN ('claimed','active')
             WHERE p.tenant_id = $1 AND p.status = 'active'
            """, tenant_id)
    return [Served(int(r["id"]), int(r["b24_group_id"]), str(r["name"]),
                   str(r["client_name"])) for r in rows]


async def _tg_by_b24(tenant_id: int) -> dict[int, int]:
    """Кто из сотрудников Битрикса доступен в Telegram. Без пары писать некому."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT m.b24_user_id, u.tg_user_id FROM tenant_members m "
            "JOIN users u ON u.id = m.user_id "
            "WHERE m.tenant_id = $1 AND m.b24_user_id IS NOT NULL "
            "AND u.tg_user_id IS NOT NULL", tenant_id)
    return {int(r["b24_user_id"]): int(r["tg_user_id"]) for r in rows}


async def _open_tasks(tenant_id: int, group_ids: list[int]
                      ) -> tuple[list[dict[str, Any]], bool] | None:
    """Незакрытые задачи проектов сервисным токеном. `None` — портал не ответил.

    Отказ портала и честное «задач нет» обязаны различаться: первое значит
    «попробуем ещё раз», второе — «сообщать нечего». Слить их в пустой список
    значит выдать молчание портала за спокойное утро.

    Токен установщика, а не личный: в девять утра и за два часа до срока никакого
    «текущего пользователя» не существует (docs/10-architecture.md §3).
    """
    if not group_ids:
        return [], True
    try:
        client = await access.client_for_service(tenant_id)
        async with client:
            return await views.fetch_open_all(client, group_ids)
    except (NeedsReauth, errors.B24Error) as exc:
        log.warning("теннант %s: задачи для напоминаний не получены: %s", tenant_id, exc)
        return None


async def deadlines_pass() -> int:
    """Личные напоминания о наступающем сроке — по всем активным теннантам."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT t.id FROM tenants t "
            "JOIN chat_bindings b ON b.tenant_id = t.id AND b.status = 'active' "
            "WHERE t.status = 'active'")
    sent = 0
    for row in rows:
        try:
            sent += await _deadlines_for_tenant(int(row["id"]))
        except Exception:
            log.exception("напоминания о сроках теннанта %s не отправлены", row["id"])
    return sent


async def _deadlines_for_tenant(tenant_id: int) -> int:
    projects = await served_projects(tenant_id)
    allowed = {p.b24_group_id: p for p in projects
               if await events.is_enabled(tenant_id, p.id, CODE_DEADLINE)}
    if not allowed:
        return 0

    snapshot = await _open_tasks(tenant_id, [p.b24_group_id for p in allowed.values()])
    if snapshot is None:
        return 0
    tasks, _ = snapshot
    if not tasks:
        return 0

    now = _now()
    horizon = now + DEADLINE_WINDOW
    people = await _tg_by_b24(tenant_id)
    if not people:
        return 0
    token = await dm.bot_token(tenant_id)
    if token is None:
        return 0

    domain = await _tenant_domain(tenant_id)
    sent = 0
    for task in tasks:
        deadline = _dt(task.get("deadline"))
        task_id = as_int(task.get("id"))
        if deadline is None or task_id is None or not (now < deadline <= horizon):
            continue
        project = allowed.get(as_int(task.get("groupId")) or 0)
        if project is None:
            continue
        tg_user_id = people.get(as_int(task.get("responsibleId")) or 0)
        if tg_user_id is None:
            continue
        # Отпечаток — сам срок: перенесли срок, значит повод новый и напомнить надо
        # заново. Не перенесли — второго сообщения об одном и том же не будет.
        if not await mark_once(tenant_id, "task", task_id, "deadline_soon",
                               str(task.get("deadline"))):
            continue

        ref = views.task_ref(task_id, domain=domain,
                             b24_user_id=as_int(task.get("responsibleId")))
        text = (f"⏰ <b>Срок через {fmt_age(deadline - now)}</b>\n\n"
                f"{ref} · {esc_html(str(task.get('title') or ''))}\n"
                f"{esc_html(project.client_name)} · {esc_html(project.name)}\n"
                f"Срок: {views.fmt_date(task.get('deadline'))}")
        if await dm.send(tenant_id, tg_user_id, text, token=token):
            sent += 1
    return sent


async def tenant_tz(tenant_id: int) -> tzinfo:
    """Часовой пояс портала теннанта. Тот же для утренней сводки и для `/digest`:
    «сегодня» обязано значить одно и то же в рассылке и в ответе на команду."""
    async with pool().acquire() as conn:
        value = await conn.fetchval("SELECT tz FROM tenants WHERE id = $1", tenant_id)
    return tz_of(value if value is None else str(value))


async def _tenant_domain(tenant_id: int) -> str:
    async with pool().acquire() as conn:
        value = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1",
                                    tenant_id)
    return str(value or "")


# ------------------------------------------------------------ утренняя сводка
@dataclass(frozen=True)
class Digest:
    total: int
    overdue: int
    today: int
    no_deadline: int
    stale: int
    awaiting: int
    complete: bool = True

    @property
    def worth_sending(self) -> bool:
        """Есть ли о чём говорить. Ежедневное «всё в порядке» в рабочем чате —
        шум, который перестают читать вместе со всем остальным."""
        return bool(self.overdue or self.today or self.awaiting or self.stale)


def build_digest(tasks: list[dict[str, Any]], *, awaiting: int, now: datetime,
                 tz: tzinfo, complete: bool = True) -> Digest:
    """Числа сводки. Чистая функция: то же самое считают и воркер, и `/digest`.

    «Срок сегодня» считается по местному календарному дню портала, а не по UTC:
    задача со сроком 31 декабря 23:30 по Москве иначе уехала бы в завтра, а
    вместе с ней и весь смысл строки.
    """
    local_end = (now.astimezone(tz).replace(hour=23, minute=59, second=59,
                                            microsecond=0))
    stale_before = now - STALE_AFTER
    overdue = today = no_deadline = stale = 0
    for task in tasks:
        deadline = _dt(task.get("deadline"))
        if deadline is None:
            no_deadline += 1
        elif deadline < now:
            overdue += 1
        elif deadline <= local_end:
            today += 1
        changed = _dt(task.get("changedDate")) or _dt(task.get("createdDate"))
        if changed is not None and changed < stale_before:
            stale += 1
    return Digest(total=len(tasks), overdue=overdue, today=today,
                  no_deadline=no_deadline, stale=stale, awaiting=awaiting,
                  complete=complete)


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")


def render_digest(digest: Digest, *, day: date, projects: list[str]) -> str:
    """Текст сводки. Заголовков задач здесь нет намеренно — см. докстринг модуля."""
    lines = [f"🌅 <b>Сводка на {day.day} {MONTHS[day.month - 1]}</b>"]
    if projects:
        lines.append(f"<i>{esc_html(' · '.join(projects))}</i>")
    lines.append("")

    if not digest.total:
        lines.append("Открытых задач нет.")
        return "\n".join(lines)

    if digest.overdue:
        lines.append(f"⏰ Просрочено: <b>{digest.overdue}</b>")
    if digest.today:
        lines.append(f"📅 Срок сегодня: <b>{digest.today}</b>")
    if digest.awaiting:
        lines.append(f"🙋 Ждут подтверждения: <b>{digest.awaiting}</b>")
    if digest.stale:
        lines.append(f"🧊 Без движения больше {STALE_AFTER.days} дней: "
                     f"<b>{digest.stale}</b>")
    if digest.no_deadline:
        lines.append(f"🗓 Без срока: <b>{digest.no_deadline}</b>")
    lines.append(f"📋 Всего открытых: <b>{digest.total}</b>")

    if not digest.complete:
        # Молчаливое усечение выглядит как баг продукта: числа занижены, а вид у
        # них такой же уверенный, как у полных.
        lines += ["", f"<i>Портал отдал первые {digest.total} задач — "
                      f"в проектах их больше, числа занижены.</i>"]
    return "\n".join(lines)


async def awaiting_count(tenant_id: int, project_ids: list[int]) -> int:
    if not project_ids:
        return 0
    async with pool().acquire() as conn:
        value = await conn.fetchval(
            "SELECT count(*) FROM task_approvals WHERE tenant_id = $1 "
            "AND status = 'pending' AND project_id = ANY($2::bigint[])",
            tenant_id, project_ids)
    return int(value or 0)


async def digest_enabled(tenant_id: int, chat_ref: int) -> bool:
    """Включена ли сводка хоть для одной привязки чата.

    Чат — не уровень настроек (их уровни: привязка, проект, теннант), а сводка
    живёт именно в чате. Поэтому «включено» здесь значит «включено хотя бы для
    одного из его проектов»: иначе включение в чате с двумя проектами зависело бы
    от того, какой из них спросили первым.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, project_id FROM chat_bindings "
            "WHERE tenant_id = $1 AND chat_ref = $2 AND status = 'active'",
            tenant_id, chat_ref)
    for row in rows:
        if await events.is_enabled(tenant_id, int(row["project_id"]), CODE_DIGEST,
                                   binding_id=int(row["id"])):
            return True
    return False


async def set_digest(tenant_id: int, chat_ref: int, enabled: bool) -> int:
    """Включить или выключить сводку для всех привязок чата.

    Пишется явное значение, а не удаляется строка: отсутствие записи означает
    «наследовать выше» (И-9), и выключение удалением вернуло бы дефолт теннанта.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM chat_bindings WHERE tenant_id = $1 AND chat_ref = $2 "
            "AND status = 'active'", tenant_id, chat_ref)
        for row in rows:
            await conn.execute(
                "INSERT INTO notification_settings (tenant_id, scope_kind, scope_id, "
                "code, enabled) VALUES ($1,'binding',$2,$3,$4) "
                "ON CONFLICT (tenant_id, scope_kind, scope_id, code) DO UPDATE "
                "SET enabled = EXCLUDED.enabled",
                tenant_id, int(row["id"]), CODE_DIGEST, enabled)
    return len(rows)


async def digest_pass() -> int:
    """Утренняя сводка по чатам, у которых наступило местное утро."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id AS chat_ref, c.tenant_id, c.bot_ref, tn.tz, tp.thread_id,
                   array_agg(DISTINCT p.id)           AS project_ids,
                   array_agg(DISTINCT p.b24_group_id) AS group_ids,
                   array_agg(DISTINCT p.name)         AS project_names
              FROM chat_bindings b
              JOIN tg_chats c ON c.id = b.chat_ref AND c.status IN ('claimed','active')
              JOIN tenants tn ON tn.id = b.tenant_id AND tn.status = 'active'
              JOIN projects p ON p.id = b.project_id AND p.status = 'active'
              LEFT JOIN tg_topics tp ON tp.id = b.topic_ref
             WHERE b.status = 'active' AND c.bot_ref IS NOT NULL
             GROUP BY c.id, c.tenant_id, c.bot_ref, tn.tz, tp.thread_id
            """)
    sent = 0
    for row in rows:
        try:
            sent += await _digest_for_chat(row)
        except Exception:
            log.exception("сводка для чата %s не собрана", row["chat_ref"])
    return sent


async def _digest_for_chat(row: Any) -> int:
    tenant_id, chat_ref = int(row["tenant_id"]), int(row["chat_ref"])
    tz = tz_of(row["tz"])
    local = _now().astimezone(tz)
    if not (DIGEST_HOUR <= local.hour < DIGEST_HOUR + DIGEST_WINDOW.seconds // 3600):
        return 0
    if not await digest_enabled(tenant_id, chat_ref):
        return 0

    thread_id = as_int(row["thread_id"])
    # Топик — отдельный адрес: у форума привязки живут по темам, и сводка обязана
    # прийти туда же, куда приходят уведомления этой привязки.
    kind = "digest" if thread_id is None else f"digest:{thread_id}"
    if not await mark_once(tenant_id, "chat", chat_ref, kind, local.date().isoformat()):
        return 0

    project_ids = [int(x) for x in row["project_ids"]]
    snapshot = await _open_tasks(tenant_id, [int(x) for x in row["group_ids"]])
    if snapshot is None:
        # Портал промолчал — вопрос «сводка за сегодня» не решён, а отложен:
        # окно ещё открыто, следующий проход через четверть часа.
        await clear_mark(tenant_id, "chat", chat_ref, kind)
        return 0
    tasks, complete = snapshot
    digest = build_digest(tasks, awaiting=await awaiting_count(tenant_id, project_ids),
                          now=_now(), tz=tz, complete=complete)
    if not digest.worth_sending:
        log.info("сводка для чата %s не отправлена: сообщать нечего", chat_ref)
        return 0

    text = render_digest(digest, day=local.date(),
                         projects=[str(x) for x in row["project_names"]])
    await events.enqueue(
        tenant_id, bot_ref=int(row["bot_ref"]), chat_ref=chat_ref, thread_id=thread_id,
        kind="digest.daily", text=text,
        markup=await digest_markup(tenant_id, chat_ref),
        dedup_key=f"digest:{chat_ref}:{thread_id or 0}:{local.date().isoformat()}")
    return 1


async def digest_markup(tenant_id: int, chat_ref: int) -> dict[str, Any]:
    """Кнопки под сводкой. Они и есть ответ на вопрос «а какие именно задачи»:
    список открывается под личным токеном нажавшего, то есть с его правами."""
    buttons = []
    for action, label in (("overdue", "⏰ Просроченные"), ("all", "📋 Все задачи")):
        token = await issue_token("menu", tenant_id=tenant_id, chat_ref=chat_ref,
                                  payload={"action": action}, single_use=False,
                                  ttl=timedelta(days=365))
        buttons.append(keyboards.cb("m", token, label))
    return keyboards.inline([buttons])


# ------------------------------------------------------------------- воркер
async def run_due() -> int:
    """Все проходы разом, каждый со своим периодом. Исключений не поднимает.

    Расписание живёт в памяти процесса: перезапуск воркера просто проверит всё
    заново, а второго сообщения от этого не будет — за это отвечают отметки в базе.
    """
    jobs = (("approvals", APPROVAL_PASS, approvals_pass),
            ("deadlines", DEADLINE_PASS, deadlines_pass),
            ("digest", DIGEST_PASS, digest_pass))
    total = 0
    for name, period, job in jobs:
        if _now() < _next_run.get(name, _EPOCH):
            continue
        # Отметка сдвигается ДО прохода: отказавший портал не должен превращать
        # периодический проход в непрерывный (тот же приём, что в domain/sync.py).
        _next_run[name] = _now() + period
        try:
            total += await job()
        except Exception:
            log.exception("проход напоминаний %r упал", name)
    return total
