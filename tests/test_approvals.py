"""Подтверждение задачи ответственным: гонка решения, права, откат при сбое портала.

Единственное, что стоит проверять на настоящей PostgreSQL: атомарный переход
pending -> confirmed/rejected и откат при сбое мутации. Цена ошибки здесь не
«неверный текст», а задача, «подтверждённая» у нас и нетронутая на портале —
и повторно решить её уже нельзя, кнопка одноразовая. Сам вызов Битрикса
подменяется (`task_service.apply_patch`) — сеть в этих тестах не участвует,
и настоящий `b24_user_tokens` не нужен: подменённая функция никогда не доходит
до `client.call()`.

Запуск: `TEST_DATABASE_URL=postgresql://user:pass@host:5432/postgres pytest`.
Без переменной модуль пропускается.
"""
from __future__ import annotations

import os
import uuid

import pytest

from b24bot.b24 import errors
from b24bot.b24.tokens import NeedsReauth

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not ADMIN_URL, reason="нужна TEST_DATABASE_URL"),
    pytest.mark.asyncio,
]


async def _world(conn: object) -> dict[str, int]:
    """Теннант, проект и ответственный с привязанным Telegram и Битриксом."""
    uniq = uuid.uuid4().hex[:8]
    tenant = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO tenants (slug, name, b24_member_id, b24_domain, status) "
        "VALUES ($1,$2,$3,$4,'active') RETURNING id",
        f"t{uniq}", "Теннант", f"member-{uniq}", f"{uniq}.bitrix24.ru")
    client_id = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO clients (tenant_id, name, status) VALUES ($1,$2,'active') "
        "RETURNING id", tenant, "Клиент")
    project = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO projects (tenant_id, client_id, b24_group_id, name, status) "
        "VALUES ($1,$2,33,'Проект','active') RETURNING id", tenant, client_id)
    tg_user_id = int(uuid.uuid4().int % 900_000_000) + 100_000_000
    user = await conn.fetchval(  # type: ignore[attr-defined]
        "INSERT INTO users (tg_user_id, display_name) VALUES ($1,$2) RETURNING id",
        tg_user_id, "Ответственный")
    await conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO tenant_members (tenant_id, user_id, role, b24_user_id, "
        "link_status) VALUES ($1,$2,'member',777,'authorized')", tenant, user)
    return {"tenant": tenant, "client": client_id, "project": project,
            "user": user, "responsible_tg": tg_user_id}


async def _approval(conn: object, w: dict[str, int], *, status: str = "pending") -> int:
    value = await conn.fetchval(  # type: ignore[attr-defined]
        """
        INSERT INTO task_approvals (tenant_id, project_id, b24_task_id, task_title,
            responsible_user_id, confirm_stage_id, confirm_stage_title,
            reject_stage_id, reject_stage_title, status)
        VALUES ($1,$2,900,'Тестовая задача',$3,10,'Подтверждена',20,'Отклонена',$4)
        RETURNING id
        """, w["tenant"], w["project"], w["user"], status)
    return int(value)


# --------------------------------------------------------------------- resolve
async def test_wrong_actor_is_forbidden_and_leaves_status_untouched(db: object) -> None:
    from b24bot.domain import approvals

    w = await _world(db)
    approval_id = await _approval(db, w)

    result = await approvals.resolve(w["tenant"], approval_id, "confirm",
                                     w["responsible_tg"] + 1)
    assert result.outcome == "forbidden"

    status = await db.fetchval(  # type: ignore[attr-defined]
        "SELECT status FROM task_approvals WHERE id = $1", approval_id)
    assert status == "pending", "чужое нажатие не должно трогать состояние"


async def test_unknown_approval_is_not_found(db: object) -> None:
    from b24bot.domain import approvals

    w = await _world(db)
    result = await approvals.resolve(w["tenant"], 10**9, "confirm", w["responsible_tg"])
    assert result.outcome == "not_found"


async def test_already_resolved_short_circuits_before_bitrix(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторный клик по уже решённой заявке не имеет права дойти до Битрикса —
    это и есть закрытие гонки двойного клика без advisory-lock."""
    from b24bot.domain import approvals

    async def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("resolve() дошёл до Битрикса на уже решённой заявке")

    monkeypatch.setattr(approvals.task_service, "apply_patch", _boom)

    w = await _world(db)
    approval_id = await _approval(db, w, status="confirmed")

    result = await approvals.resolve(w["tenant"], approval_id, "confirm",
                                     w["responsible_tg"])
    assert result.outcome == "already_done"


async def test_confirm_moves_task_and_writes_high_risk_audit(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from b24bot.domain import approvals

    applied: list[dict[str, object]] = []

    async def _fake_apply_patch(client: object, tenant_id: int, task_id: int,
                                patch: dict[str, object], *,
                                actor_b24_user_id: int
                                ) -> tuple[dict[str, object], list[str]]:
        applied.append({"task_id": task_id, "patch": dict(patch),
                        "actor": actor_b24_user_id})
        return {"id": task_id}, []

    monkeypatch.setattr(approvals.task_service, "apply_patch", _fake_apply_patch)

    w = await _world(db)
    approval_id = await _approval(db, w)

    result = await approvals.resolve(w["tenant"], approval_id, "confirm",
                                     w["responsible_tg"])

    assert result.outcome == "confirmed"
    assert result.stage_title == "Подтверждена"
    assert applied == [{"task_id": 900, "patch": {"stage_id": 10}, "actor": 777}]

    row = await db.fetchrow(  # type: ignore[attr-defined]
        "SELECT status, resolved_at FROM task_approvals WHERE id = $1", approval_id)
    assert row["status"] == "confirmed"
    assert row["resolved_at"] is not None

    audited = await db.fetchrow(  # type: ignore[attr-defined]
        "SELECT action, high_risk FROM audit_log WHERE tenant_id = $1 AND target = $2",
        w["tenant"], "task:900")
    assert audited["action"] == "task.approval.confirm"
    assert audited["high_risk"] is True


async def test_reject_targets_the_reject_stage(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from b24bot.domain import approvals

    applied: list[dict[str, object]] = []

    async def _fake_apply_patch(client: object, tenant_id: int, task_id: int,
                                patch: dict[str, object], *,
                                actor_b24_user_id: int
                                ) -> tuple[dict[str, object], list[str]]:
        applied.append(dict(patch))
        return {"id": task_id}, []

    monkeypatch.setattr(approvals.task_service, "apply_patch", _fake_apply_patch)

    w = await _world(db)
    approval_id = await _approval(db, w)

    result = await approvals.resolve(w["tenant"], approval_id, "reject",
                                     w["responsible_tg"])
    assert result.outcome == "rejected"
    assert result.stage_title == "Отклонена"
    assert applied == [{"stage_id": 20}]


async def test_bitrix_failure_rolls_back_to_pending(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Задача не должна остаться «подтверждённой» у нас и нетронутой на портале —
    иначе решить её повторно уже нельзя: кнопка в личке одноразовая."""
    from b24bot.domain import approvals

    async def _fail(*args: object, **kwargs: object) -> object:
        raise errors.B24Error("SOME_ERROR", "портал недоступен")

    monkeypatch.setattr(approvals.task_service, "apply_patch", _fail)

    w = await _world(db)
    approval_id = await _approval(db, w)

    result = await approvals.resolve(w["tenant"], approval_id, "reject",
                                     w["responsible_tg"])
    assert result.outcome == "b24_error"

    row = await db.fetchrow(  # type: ignore[attr-defined]
        "SELECT status, resolved_at FROM task_approvals WHERE id = $1", approval_id)
    assert row["status"] == "pending", "статус обязан вернуться в pending"
    assert row["resolved_at"] is None


async def test_needs_reauth_rolls_back_to_pending(
        db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from b24bot.domain import approvals

    async def _fail(*args: object, **kwargs: object) -> object:
        raise NeedsReauth(0, 0, "токен истёк")

    monkeypatch.setattr(approvals.task_service, "apply_patch", _fail)

    w = await _world(db)
    approval_id = await _approval(db, w)

    result = await approvals.resolve(w["tenant"], approval_id, "confirm",
                                     w["responsible_tg"])
    assert result.outcome == "needs_reauth"

    status = await db.fetchval(  # type: ignore[attr-defined]
        "SELECT status FROM task_approvals WHERE id = $1", approval_id)
    assert status == "pending"


# ---------------------------------------------------------------- on_task_created
async def test_on_task_created_is_idempotent_per_task(db: object) -> None:
    """Хук может в теории сработать дважды на один task_id — не должно быть двух
    параллельных заявок (это защищает частичный уникальный индекс, не Python)."""
    from b24bot.domain import approvals
    from b24bot.domain.context import ProjectRef

    w = await _world(db)
    await approvals.upsert_settings(
        w["tenant"], w["project"], enabled=True, responsible_user_id=w["user"],
        confirm_stage_id=10, confirm_stage_title="Подтверждена",
        reject_stage_id=20, reject_stage_title="Отклонена")

    project = ProjectRef(id=w["project"], b24_group_id=33, name="Проект",
                         client_name="Клиент")
    task = {"id": 901, "title": "Дубль"}

    await approvals.on_task_created(w["tenant"], project, task)
    await approvals.on_task_created(w["tenant"], project, task)

    count = await db.fetchval(  # type: ignore[attr-defined]
        "SELECT count(*) FROM task_approvals WHERE tenant_id = $1 AND b24_task_id = 901",
        w["tenant"])
    assert count == 1, "хук создал вторую параллельную заявку на ту же задачу"


async def test_on_task_created_skips_when_not_configured(db: object) -> None:
    from b24bot.domain import approvals
    from b24bot.domain.context import ProjectRef

    w = await _world(db)
    project = ProjectRef(id=w["project"], b24_group_id=33, name="Проект",
                         client_name="Клиент")
    await approvals.on_task_created(w["tenant"], project, {"id": 902, "title": "Т"})

    count = await db.fetchval(  # type: ignore[attr-defined]
        "SELECT count(*) FROM task_approvals WHERE tenant_id = $1", w["tenant"])
    assert count == 0


async def test_on_task_created_skips_when_settings_incomplete(db: object) -> None:
    """enabled=true с недобранными полями — переходное состояние формы, не повод падать."""
    from b24bot.domain import approvals
    from b24bot.domain.context import ProjectRef

    w = await _world(db)
    await approvals.upsert_settings(
        w["tenant"], w["project"], enabled=True, responsible_user_id=None,
        confirm_stage_id=None, confirm_stage_title="", reject_stage_id=None,
        reject_stage_title="")

    project = ProjectRef(id=w["project"], b24_group_id=33, name="Проект",
                         client_name="Клиент")
    await approvals.on_task_created(w["tenant"], project, {"id": 903, "title": "Т"})

    count = await db.fetchval(  # type: ignore[attr-defined]
        "SELECT count(*) FROM task_approvals WHERE tenant_id = $1", w["tenant"])
    assert count == 0


# -------------------------------------------------------------------- pending_for
async def test_pending_for_reports_honest_total_when_truncated(db: object) -> None:
    from b24bot.domain import approvals

    w = await _world(db)
    for i in range(3):
        await db.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO task_approvals (tenant_id, project_id, b24_task_id, task_title,
                responsible_user_id, confirm_stage_id, confirm_stage_title,
                reject_stage_id, reject_stage_title, status)
            VALUES ($1,$2,$3,$4,$5,10,'Подтверждена',20,'Отклонена','pending')
            """,
            w["tenant"], w["project"], 950 + i, f"Задача {i}", w["user"])

    items, total = await approvals.pending_for(w["tenant"], w["user"], limit=2)
    assert total == 3
    assert len(items) == 2, "лимит обязан урезать выдачу, не только счётчик"
