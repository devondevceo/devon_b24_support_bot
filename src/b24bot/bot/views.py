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
from b24bot.core.text import esc_html
from b24bot.db.pool import pool
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

        counted = 0
        for stage_id, title in stages:
            n = sum(1 for t in mine if as_int(t.get("stageId")) == stage_id)
            counted += n
            if n:
                lines.append(f"  {esc_html(title)} — {n}")
        outside = len(mine) - counted
        if outside:
            # Задачи вне канбана: STAGE_ID=0. Показываем честно, а не прячем.
            lines.append(f"  Вне канбана — {outside}")

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


def render_list(tasks: list[dict[str, Any]], *, title: str, page: int = 0) -> str:
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
        lines.append(f"{pad}{mark} <b>{i}.</b> {branch}#{t.get('id')} "
                     f"{esc_html(t.get('title') or '')}")
        lines.append(f"{pad}     {esc_html(who)} · {esc_html(deadline)}")
    return "\n".join(lines)


def flatten_for_buttons(tasks: list[dict[str, Any]], page: int = 0
                        ) -> list[dict[str, Any]]:
    """Тот же порядок, что в тексте: номер кнопки обязан совпадать со строкой."""
    ordered = [t for t, _ in order_by_hierarchy(tasks)]
    return ordered[page * PAGE:(page + 1) * PAGE]


def render_card(task: dict[str, Any], project: ProjectRef) -> str:
    status = as_int(task.get("status"))
    stage = task.get("stageId")
    overdue = is_overdue(task)

    rows = [
        f"<b>#{task.get('id')} · {esc_html(task.get('title') or '')}</b>",
        f"Клиент: {esc_html(project.client_name)} · Проект: {esc_html(project.name)}",
        f"Статус: {STATUS_EMOJI.get(status or 0, '•')} "
        f"{esc_html(STATUS_TITLES.get(status or 0, '—'))}"
        + (" 🔥 просрочена" if overdue else ""),
    ]
    if stage and as_int(stage):
        rows.append(f"Стадия: {esc_html(str(task.get('stageTitle') or stage))}")
    rows.append(f"Ответственный: "
                f"{esc_html((task.get('responsible') or {}).get('name') or '—')}")
    rows.append(f"Постановщик: {esc_html((task.get('creator') or {}).get('name') or '—')}")
    rows.append(f"Создана: {fmt_date(task.get('createdDate'))}")
    if task.get("deadline"):
        rows.append(f"Срок: {fmt_date(task.get('deadline'))}")

    description = str(task.get("description") or "").strip()
    if description:
        short = description[:400] + ("…" if len(description) > 400 else "")
        rows += ["", esc_html(short)]
    return "\n".join(rows)


def portal_task_url(domain: str, task_id: int, b24_user_id: int) -> str:
    return f"https://{domain}/company/personal/user/{b24_user_id}/tasks/task/view/{task_id}/"
