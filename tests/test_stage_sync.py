"""Синхронизация стадий канбана.

Повод — разлад справочника с порталом. Стадии заполнялись однократно, при импорте
проекта, а дальше владелец проекта заводил колонку в Битриксе, и её задачи
превращались в сводке в строку «Вне канбана»: та же строка, что у задач с честным
`STAGE_ID=0`, и различить их человек не мог никак.

Разбор ответа проверяется без базы, остальное — на настоящей PostgreSQL: цена
ошибки здесь не «неверный текст», а стёртый справочник или чужие стадии в проекте.
Запуск — docs/20-data-model.md §13.1.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from b24bot.b24.client import B24Client
from b24bot.b24.limiter import PortalLimiter
from b24bot.domain import sync
from tests.fake_portal import FakePortal, kanban_stages

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")


# --------------------------------------------------------------- разбор ответа
def test_parse_reads_the_portal_shape() -> None:
    """Ответ приходит объектом, ключ — id стадии, а не массивом (§3.2)."""
    stages = sync.parse_stages(kanban_stages(), 33)

    assert [s.b24_stage_id for s in stages] == [333, 335, 337]
    assert [s.title for s in stages] == ["Новые", "Выполняются", "Сделаны"]
    assert stages[0].system_type == "NEW"
    # Пустая строка в SYSTEM_TYPE — это отсутствие типа, а не тип с именем "".
    assert stages[1].system_type is None


def test_parse_accepts_a_list_too() -> None:
    """Форма ответа портала уже стоила нам ошибок — принимаем обе."""
    stages = sync.parse_stages(list(kanban_stages().values()), 33)
    assert [s.b24_stage_id for s in stages] == [333, 335, 337]


def test_parse_orders_by_sort_not_by_id() -> None:
    """Порядок колонок задаёт владелец проекта, и сводка обязана его повторять."""
    raw = kanban_stages()
    raw["337"]["SORT"] = "50"
    assert [s.b24_stage_id for s in sync.parse_stages(raw, 33)] == [337, 333, 335]


def test_parse_drops_stages_of_another_entity() -> None:
    """Стадия с чужим `ENTITY_ID` в проект попасть не должна.

    Пустить такую колонку — значит показать клиенту чужие стадии в его
    собственной сводке.
    """
    raw = kanban_stages()
    raw["901"] = {"ID": "901", "TITLE": "Личное", "SORT": "10",
                  "ENTITY_ID": "77", "ENTITY_TYPE": "U"}
    assert [s.b24_stage_id for s in sync.parse_stages(raw, 33)] == [333, 335, 337]


def test_parse_survives_a_stage_without_a_title() -> None:
    """`title` в базе NOT NULL: пустое имя не имеет права уронить синхронизацию."""
    raw = kanban_stages()
    raw["335"]["TITLE"] = ""
    titles = {s.b24_stage_id: s.title for s in sync.parse_stages(raw, 33)}
    assert titles[335] == "Стадия 335"


def test_parse_ignores_junk() -> None:
    assert sync.parse_stages(None, 33) == []
    assert sync.parse_stages({"x": "не словарь"}, 33) == []
    assert sync.parse_stages({"0": {"TITLE": "без ID"}}, 33) == []


def test_unknown_stages_does_not_count_zero() -> None:
    """`STAGE_ID=0` — это «вне канбана», законное состояние, а не незнакомая стадия."""
    from b24bot.bot.views import _unknown_stages

    tasks: list[dict[str, Any]] = [{"stageId": "0"}, {"stageId": "333"},
                                   {"stageId": "999"}, {"stageId": None}]
    assert _unknown_stages(tasks, [(333, "Новые")]) == {999}
    assert _unknown_stages(tasks, [(333, "Новые"), (999, "Новая колонка")]) == set()


# ------------------------------------------------------------- на живой базе
# Метки навешиваются на каждый тест отдельно, а не модулем: разбор ответа выше
# базы не требует и обязан краснеть на любой машине.
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL")


def make_client(portal: FakePortal) -> B24Client:
    http = httpx.AsyncClient(transport=portal.transport, timeout=5)
    return B24Client("devondev.bitrix24.ru", _token,
                     PortalLimiter(rate=1000, capacity=1000), http=http)


async def _token() -> str:
    return "fake-access-token"


async def _two_tenants(conn: Any) -> dict[str, int]:
    """Два теннанта с одинаковым `b24_group_id`: порталы разные, группа одна и та же.

    Ровно та форма данных, на которой ошибка в `WHERE` уводит стадии не туда.
    """
    uniq = uuid.uuid4().hex[:8]
    ids: dict[str, int] = {}
    for n, tag in ((1, "a"), (2, "b")):
        t = await conn.fetchval(
            "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
            "VALUES ($1,$2,$3,$4,'active') RETURNING id",
            f"t{uniq}{tag}", f"Теннант {tag}", f"member-{uniq}-{tag}",
            f"t{uniq}{n}.bitrix24.ru")
        c = await conn.fetchval(
            "INSERT INTO clients (tenant_id, name, status) "
            "VALUES ($1,$2,'active') RETURNING id", t, f"Клиент {tag}")
        p = await conn.fetchval(
            "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
            "VALUES ($1,$2,33,$3,'active') RETURNING id", t, c, f"Проект {tag}")
        ids |= {f"tenant_{tag}": t, f"client_{tag}": c, f"project_{tag}": p}
    return ids


@pytest.mark.asyncio
@needs_db
async def test_sync_writes_stages_and_marks_the_project(db: Any) -> None:
    w = await _two_tenants(db)
    portal = FakePortal()

    stored = await sync.sync_project_stages(
        w["tenant_a"], w["project_a"], 33, client=make_client(portal))

    assert stored == 3
    rows = await db.fetch(
        "SELECT b24_stage_id, title, sort FROM project_stages "
        "WHERE tenant_id = $1 AND project_id = $2 ORDER BY sort",
        w["tenant_a"], w["project_a"])
    assert [r["title"] for r in rows] == ["Новые", "Выполняются", "Сделаны"]

    # Без отметки плановый проход перечитывал бы этот проект каждые 15 минут.
    marked = await db.fetchval("SELECT stages_synced_at FROM projects WHERE id = $1",
                               w["project_a"])
    assert marked is not None

    # Соседний теннант с той же группой 33 не должен получить ни одной строки.
    assert await db.fetchval(
        "SELECT count(*) FROM project_stages WHERE tenant_id = $1", w["tenant_b"]) == 0


@pytest.mark.asyncio
@needs_db
async def test_rename_is_picked_up_and_vanished_stage_is_removed(db: Any) -> None:
    """Портал — источник правды: переименование доезжает, удалённая колонка исчезает."""
    w = await _two_tenants(db)
    portal = FakePortal()
    await sync.sync_project_stages(w["tenant_a"], w["project_a"], 33,
                                   client=make_client(portal))

    portal.stages = kanban_stages((333, "Новые"), (335, "В работе"))
    stored = await sync.sync_project_stages(w["tenant_a"], w["project_a"], 33,
                                            client=make_client(portal))

    assert stored == 2
    rows = await db.fetch(
        "SELECT b24_stage_id, title FROM project_stages WHERE tenant_id = $1 "
        "AND project_id = $2 ORDER BY b24_stage_id", w["tenant_a"], w["project_a"])
    assert [(r["b24_stage_id"], r["title"]) for r in rows] == [(333, "Новые"),
                                                               (335, "В работе")]


@pytest.mark.asyncio
@needs_db
async def test_empty_answer_does_not_wipe_the_reference(db: Any) -> None:
    """Пустой ответ — это «портал промолчал», а не «колонок больше нет».

    Стереть справочник по такому ответу значит превратить всю сводку проекта
    в строку «Вне канбана» — ровно тот баг, ради которого написан этот модуль.
    """
    w = await _two_tenants(db)
    portal = FakePortal()
    await sync.sync_project_stages(w["tenant_a"], w["project_a"], 33,
                                   client=make_client(portal))

    portal.stages = {}
    assert await sync.sync_project_stages(w["tenant_a"], w["project_a"], 33,
                                          client=make_client(portal)) is None

    assert await db.fetchval(
        "SELECT count(*) FROM project_stages WHERE tenant_id = $1 AND project_id = $2",
        w["tenant_a"], w["project_a"]) == 3


@pytest.mark.asyncio
@needs_db
async def test_due_selection_respects_ttl_and_project_status(db: Any) -> None:
    """Кого берёт плановый проход: несинхронизированных и просроченных, но не свежих."""
    w = await _two_tenants(db)
    ids = {int(r["id"]) for r in await sync.due_projects(50)}
    # Ни один проект ещё не синхронизирован — оба обязаны быть в очереди.
    assert {w["project_a"], w["project_b"]} <= ids

    await db.execute("UPDATE projects SET stages_synced_at = now() WHERE id = $1",
                     w["project_a"])
    ids = {int(r["id"]) for r in await sync.due_projects(50)}
    assert w["project_a"] not in ids and w["project_b"] in ids

    stale = datetime.now(UTC) - sync.STAGE_TTL - timedelta(minutes=1)
    await db.execute("UPDATE projects SET stages_synced_at = $2 WHERE id = $1",
                     w["project_a"], stale)
    assert w["project_a"] in {int(r["id"]) for r in await sync.due_projects(50)}

    # Архивный проект синхронизировать незачем: его задачи мы уже не показываем.
    await db.execute("UPDATE projects SET status = 'archived' WHERE id = $1",
                     w["project_a"])
    assert w["project_a"] not in {int(r["id"]) for r in await sync.due_projects(50)}


@pytest.mark.asyncio
@needs_db
async def test_summary_names_the_new_stage_instead_of_calling_it_outside(
        db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Сквозная проверка исходного бага.

    Колонку завели в Битриксе после импорта проекта. Задача на ней обязана
    попасть в сводку под своим именем, а не в «Вне канбана» до завтрашнего прохода.
    """
    from b24bot.bot import views
    from b24bot.domain.context import ProjectRef

    w = await _two_tenants(db)
    portal = FakePortal(stages=kanban_stages((333, "Новые")))
    await sync.sync_project_stages(w["tenant_a"], w["project_a"], 33,
                                   client=make_client(portal))

    # Владелец проекта добавил колонку и перетащил в неё задачу.
    portal.stages = kanban_stages((333, "Новые"), (400, "На проверке"))
    await db.execute("UPDATE projects SET stages_synced_at = NULL WHERE id = $1",
                     w["project_a"])
    sync._failed_until.clear()

    async def fake_service(tenant_id: int) -> B24Client:
        return make_client(portal)

    monkeypatch.setattr(sync.access, "client_for_service", fake_service)

    project = ProjectRef(id=w["project_a"], b24_group_id=33,
                         name="Проект a", client_name="Клиент a")
    tasks = [{"id": "1", "groupId": "33", "stageId": "400", "status": "2"},
             {"id": "2", "groupId": "33", "stageId": "0", "status": "2"}]

    text = await views.render_summary(w["tenant_a"], [project], tasks)

    assert "На проверке — 1" in text
    assert "Вне канбана — 1" in text
    assert "Стадия не опознана" not in text
