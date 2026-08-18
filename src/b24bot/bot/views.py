"""Отрисовка сводок, списков и карточек задач.

Сводка строится по СТАДИЯМ проекта, а не по статусам: то, что человек видит в
Битриксе как «Новые», это стадия канбана, у каждого проекта своя. Статус и стадия
независимы — проверено на портале (docs/00-portal-facts.md §3.2).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from b24bot.b24.client import B24Client
from b24bot.b24.mapping import (
    STATUS_DONE,
    STATUS_EMOJI,
    STATUS_TITLES,
    TASK_SELECT_LIST,
    as_int,
)
from b24bot.core.text import bbcode_to_text, esc_html
from b24bot.db.pool import pool
from b24bot.domain import sync
from b24bot.domain.context import ProjectRef

log = logging.getLogger(__name__)

PAGE = 10


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(dt: Any) -> datetime | None:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(str(dt))
    except ValueError:
        return None


def is_overdue(task: dict[str, Any]) -> bool:
    deadline = _parse(task.get("deadline"))
    status = as_int(task.get("status"))
    return bool(deadline and status != STATUS_DONE and deadline < _now())


def fmt_duration(seconds: Any) -> str:
    """«5 ч 30 мин». Ноль и пустое — прочерк, а не «0 ч».

    Портал отдаёт секунды строкой, а у задачи без списаний поле приходит `null`,
    и это ровно то же самое, что ноль: работали ноль времени.
    """
    total = as_int(seconds) or 0
    if total <= 0:
        return "—"
    hours, minutes = divmod(round(total / 60), 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    return f"{hours} ч" if hours else f"{minutes} мин"


def fmt_date(value: Any) -> str:
    dt = _parse(value)
    if dt is None:
        return "—"
    return dt.strftime("%d.%m %H:%M") if dt.year == _now().year else dt.strftime("%d.%m.%Y")


async def fetch_open(client: B24Client, group_ids: list[int]) -> list[dict[str, Any]]:
    """Незакрытые задачи разрешённых проектов.

    `GROUP_ID` — обязательный аргумент, а не удобная опция: сервисной учётки с
    урезанной видимостью у нас нет, и область выборки задаёт только этот фильтр
    (docs/40-security.md §1).
    """
    if not group_ids:
        return []
    res = await client.call("tasks.task.list", {
        "filter": {"GROUP_ID": group_ids, "!=REAL_STATUS": STATUS_DONE},
        "select": TASK_SELECT_LIST,
        "order": {"DEADLINE": "asc"},
    })
    tasks = res.get("tasks", []) if isinstance(res, dict) else []
    return [t for t in tasks if isinstance(t, dict)]


async def stages_of(tenant_id: int, project_id: int) -> list[tuple[int, str]]:
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT b24_stage_id, title FROM project_stages "
            "WHERE tenant_id = $1 AND project_id = $2 ORDER BY sort",
            tenant_id, project_id)
    return [(int(r["b24_stage_id"]), r["title"]) for r in rows]


OUTSIDE_KANBAN = "Вне канбана"
UNKNOWN_STAGE = "Стадия не опознана"


def stage_label(stage_id: Any, titles: dict[int, str]) -> str:
    """Название стадии по справочнику. Два разных смысла — две разные строки.

    `STAGE_ID=0` значит «задача не разложена по колонкам» и это нормальное
    состояние. Стадия, которой нет в справочнике, — уже наш разлад с порталом,
    и молчать о нём значит показывать неверную сводку с уверенным видом.
    """
    sid = as_int(stage_id) or 0
    if not sid:
        return OUTSIDE_KANBAN
    return titles.get(sid) or UNKNOWN_STAGE


async def resolve_stage_title(tenant_id: int, project: ProjectRef,
                              stage_id: Any) -> str:
    """То же самое, но со справочником из базы и починкой на месте.

    Незнакомая стадия почти всегда означает колонку, заведённую в Битриксе после
    нашей последней синхронизации. Ждать суточного прохода нельзя — человек
    смотрит на карточку сейчас.
    """
    sid = as_int(stage_id) or 0
    if not sid:
        return OUTSIDE_KANBAN
    titles = dict(await stages_of(tenant_id, project.id))
    if sid not in titles and await sync.ensure_fresh(tenant_id, project.id,
                                                     project.b24_group_id):
        titles = dict(await stages_of(tenant_id, project.id))
    return stage_label(sid, titles)


def _unknown_stages(tasks: list[dict[str, Any]], stages: list[tuple[int, str]]) -> set[int]:
    """Стадии задач, которых нет в нашем справочнике. Ноль не считается: это «вне канбана»."""
    known = {stage_id for stage_id, _ in stages}
    seen = {as_int(t.get("stageId")) or 0 for t in tasks}
    return {s for s in seen if s and s not in known}


async def render_summary(tenant_id: int, projects: list[ProjectRef],
                         tasks: list[dict[str, Any]]) -> str:
    """Сводка: сколько задач на каждой стадии проекта плюс просроченные."""
    if not tasks:
        head = " · ".join(esc_html(p.name) for p in projects)
        return f"📊 <b>{head}</b>\n\nОткрытых задач нет."

    lines = []
    for project in projects:
        mine = [t for t in tasks if str(t.get("groupId")) == str(project.b24_group_id)]
        stages = await stages_of(tenant_id, project.id)
        lines.append(f"📊 <b>{esc_html(project.client_name)} · "
                     f"{esc_html(project.name)}</b>")
        lines.append(f"Открытых: {len(mine)}")

        # Незнакомая стадия почти всегда означает одно: колонку завели в Битриксе
        # после нашей последней синхронизации. Ждать суточного прохода нельзя —
        # человек смотрит на сводку сейчас, и её задачи числились бы «вне канбана».
        if _unknown_stages(mine, stages) and await sync.ensure_fresh(
                tenant_id, project.id, project.b24_group_id):
            stages = await stages_of(tenant_id, project.id)

        counted = 0
        for stage_id, title in stages:
            n = sum(1 for t in mine if as_int(t.get("stageId")) == stage_id)
            counted += n
            if n:
                lines.append(f"  {esc_html(title)} — {n}")

        # Две разные вещи, и путать их нельзя. STAGE_ID=0 — задача действительно не
        # разложена по канбану, это нормальное состояние. Стадия, которой нет в
        # справочнике даже после обновления, — уже наш разлад с порталом, и молчать
        # о нём значит показывать неверную сводку с уверенным видом.
        outside = sum(1 for t in mine if not as_int(t.get("stageId")))
        if outside:
            lines.append(f"  {OUTSIDE_KANBAN} — {outside}")
        unresolved = len(mine) - counted - outside
        if unresolved:
            lines.append(f"  {UNKNOWN_STAGE} — {unresolved}")

        overdue = sum(1 for t in mine if is_overdue(t))
        if overdue:
            lines.append(f"  🔥 Просрочено — {overdue}")
        lines.append("")

    return "\n".join(lines).strip()


def order_by_hierarchy(tasks: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """Расставить задачи деревом: подзадача идёт под своим родителем.

    Битрикс отдаёт плоский список, а связь лежит в PARENT_ID. Если родителя нет
    в выборке (он закрыт или в другом проекте), подзадача остаётся на верхнем
    уровне — терять её нельзя.
    """
    by_id = {str(t.get("id")): t for t in tasks}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []

    for t in tasks:
        parent = str(t.get("parentId") or "0")
        if parent != "0" and parent in by_id:
            children.setdefault(parent, []).append(t)
        else:
            roots.append(t)

    out: list[tuple[dict[str, Any], int]] = []

    def walk(node: dict[str, Any], depth: int) -> None:
        out.append((node, depth))
        for child in children.get(str(node.get("id")), []):
            walk(child, min(depth + 1, 2))

    for root in roots:
        walk(root, 0)
    return out


def render_list(tasks: list[dict[str, Any]], *, title: str, page: int = 0,
                domain: str | None = None, b24_user_id: Any = None) -> str:
    if not tasks:
        return f"<b>{esc_html(title)}</b>\n\nНичего не найдено."

    ordered = order_by_hierarchy(tasks)
    total_pages = max(1, (len(ordered) + PAGE - 1) // PAGE)
    chunk = ordered[page * PAGE:(page + 1) * PAGE]
    lines = [f"<b>{esc_html(title)}</b>  <i>стр. {page + 1}/{total_pages}</i>", ""]

    for i, (t, depth) in enumerate(chunk, start=1):
        status = as_int(t.get("status"))
        mark = "🔥" if is_overdue(t) else STATUS_EMOJI.get(status or 0, "•")
        who = (t.get("responsible") or {}).get("name") or "не назначен"
        deadline = fmt_date(t.get("deadline")) if t.get("deadline") else "без срока"
        pad = "    " * depth
        branch = "└ " if depth else ""
        ref = task_ref(t.get("id"), domain=domain, b24_user_id=b24_user_id)
        lines.append(f"{pad}{mark} <b>{i}.</b> {branch}{ref} "
                     f"{esc_html(t.get('title') or '')}")
        lines.append(f"{pad}     {esc_html(who)} · {esc_html(deadline)}")
    return "\n".join(lines)


def flatten_for_buttons(tasks: list[dict[str, Any]], page: int = 0
                        ) -> list[dict[str, Any]]:
    """Тот же порядок, что в тексте: номер кнопки обязан совпадать со строкой."""
    ordered = [t for t, _ in order_by_hierarchy(tasks)]
    return ordered[page * PAGE:(page + 1) * PAGE]


def render_card(task: dict[str, Any], project: ProjectRef, *,
                domain: str | None = None, b24_user_id: Any = None,
                stage_title: str | None = None) -> str:
    """`stage_title` — уже разрешённое название стадии (`resolve_stage_title`).

    Своего названия портал в задаче не отдаёт: в ответе только `stageId`, и без
    справочника в карточке стоял голый номер колонки.
    """
    status = as_int(task.get("status"))
    overdue = is_overdue(task)
    ref = task_ref(task.get("id"), domain=domain, b24_user_id=b24_user_id)

    rows = [
        f"<b>{ref} · {esc_html(task.get('title') or '')}</b>",
        f"Клиент: {esc_html(project.client_name)} · Проект: {esc_html(project.name)}",
        f"Статус: {STATUS_EMOJI.get(status or 0, '•')} "
        f"{esc_html(STATUS_TITLES.get(status or 0, '—'))}"
        + (" 🔥 просрочена" if overdue else ""),
    ]
    if stage_title:
        rows.append(f"Стадия: {esc_html(stage_title)}")
    rows.append(f"Ответственный: "
                f"{esc_html((task.get('responsible') or {}).get('name') or '—')}")
    rows.append(f"Постановщик: {esc_html((task.get('creator') or {}).get('name') or '—')}")
    # Сумма списаний по задаче. Второго запроса не нужно: портал держит её в самой
    # задаче (docs/00-portal-facts.md §5.2), у задачи без списаний поле — null.
    rows.append(f"Трудозатраты: {fmt_duration(task.get('timeSpentInLogs'))}")
    rows.append(f"Создана: {fmt_date(task.get('createdDate'))}")
    if task.get("deadline"):
        rows.append(f"Срок: {fmt_date(task.get('deadline'))}")

    description = bbcode_to_text(task.get("description") or "")
    if description:
        short = description[:400] + ("…" if len(description) > 400 else "")
        rows += ["", esc_html(short)]
    return "\n".join(rows)


def render_timesheet(report: Any, projects: list[ProjectRef]) -> str:
    """Отчёт по трудозатратам: два разреза одной суммы.

    Разрезы обязаны сходиться между собой и с итогом — у задачи один статус и
    одна стадия. Если когда-нибудь разойдутся, это будет означать потерю времени
    по дороге, поэтому итог печатается один и считается один раз.
    """
    head = " · ".join(esc_html(p.name) for p in projects) or "проекты чата"
    lines = [f"⏱ <b>Трудозатраты · {esc_html(report.title)}</b>", head, ""]

    if not report.entry_count:
        lines.append("За этот месяц списаний времени нет.")
        return "\n".join(lines)

    lines.append("<b>По статусам</b>")
    for bucket in report.by_status:
        lines.append(f"  {esc_html(bucket.title)} — {fmt_duration(bucket.seconds)}"
                     f" · {_tasks_word(len(bucket.tasks))}")
    lines += ["", "<b>По стадиям</b>"]
    for bucket in report.by_stage:
        lines.append(f"  {esc_html(bucket.title)} — {fmt_duration(bucket.seconds)}"
                     f" · {_tasks_word(len(bucket.tasks))}")

    lines += ["", f"<b>Итого: {fmt_duration(report.total_seconds)}</b> · "
                  f"{_tasks_word(report.task_count)} · "
                  f"{plural(report.entry_count, 'списание', 'списания', 'списаний')}"]
    if not report.complete:
        # Молчаливое усечение выглядит как баг продукта. Портал отдаёт не больше
        # 50 записей за раз и не умеет листать (docs/00-portal-facts.md §5.2).
        lines.append(f"\n⚠️ Портал отдал {report.seen} записей учёта времени из "
                     f"{report.total_on_portal}. Сумма — это минимум, а не точное "
                     f"значение.")
    return "\n".join(lines)


def plural(n: int, one: str, few: str, many: str) -> str:
    """«1 задача», «2 задачи», «5 задач». Русский счёт, а не «5 задача(и)»."""
    tail = n % 100
    if 11 <= tail <= 14:
        return f"{n} {many}"
    tail %= 10
    if tail == 1:
        return f"{n} {one}"
    return f"{n} {few}" if 2 <= tail <= 4 else f"{n} {many}"


def _tasks_word(n: int) -> str:
    return plural(n, "задача", "задачи", "задач")


def portal_task_url(domain: str, task_id: int, b24_user_id: int) -> str:
    """Канонический адрес задачи на портале.

    Портал понимает две формы — личную (`/company/personal/user/<id>/tasks/…`) и
    групповую (`/workgroups/group/<id>/tasks/…`): проверено 18.08.2026 запросом,
    у обеих в редиректе на авторизацию сохраняется параметр `any=` с разобранным
    путём, а у выдуманного пути его нет (docs/00-portal-facts.md §12.2). Берём
    личную: это та же форма, что Битрикс24 ставит в собственные уведомления,
    поэтому её точно ловит мобильное приложение.
    """
    return f"https://{domain}/company/personal/user/{b24_user_id}/tasks/task/view/{task_id}/"


def task_ref(task_id: Any, *, domain: str | None = None,
             b24_user_id: Any = None) -> str:
    """«#233» ссылкой на задачу портала — или тем же текстом, если контекста нет.

    Пользователь в адресе — это контекст раздела, а не проверка прав: доступ к
    самой задаче портал проверяет отдельно. Поэтому подставляем того, кто ближе
    всего к читателю: в карточке и списке — его самого, в уведомлении в чат —
    ответственного по задаче.

    Без домена или без пользователя ссылки не выйдет — тогда остаётся обычный
    номер. Молча пропасть номер не имеет права: по нему открывают карточку.
    """
    number = f"#{task_id}"
    uid = as_int(b24_user_id)
    tid = as_int(task_id)
    if not domain or not uid or not tid:
        return number
    return f'<a href="{esc_html(portal_task_url(domain, tid, uid))}">{number}</a>'
