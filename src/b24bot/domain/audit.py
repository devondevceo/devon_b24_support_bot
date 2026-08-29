"""Запись в журнал действий.

Правило простое: если действие меняет чьи-то права, привязки или состояние задачи —
оно пишется сюда. `high_risk` (docs/40-security.md §3) пишется с полным `detail`
всегда, независимо от будущего режима аудита.

Запись аудита **не имеет права уронить действие**. Если журнал недоступен, действие
уже совершилось, и откатывать его поздно — поэтому ошибка ловится и логируется как
ошибка уровня ERROR, чтобы её было видно алертом.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from b24bot.db.pool import pool

log = logging.getLogger(__name__)

HIGH_RISK = frozenset({
    "role.grant", "role.revoke",
    "chat.bind", "chat.unbind",
    "bot.token.set", "bot.suspend",
    "user.map",
    "task.responsible.change", "task.complete", "task.defer",
    "task.approval.confirm", "task.approval.reject",
    # Разовый проход правит ТЕГИ СОТЕН чужих задач одним нажатием. Смена самого
    # тега рядом: она бесшумно меняет, что попадает в отчёт по трудозатратам,
    # и вопрос «почему цифры другие» без этой записи не разобрать.
    "tenant.support_tag.set", "tenant.support_tag.backfill",
    "report.export",
})


async def record(tenant_id: int, action: str, *,
                 actor_kind: str = "user",
                 actor_id: int | None = None,
                 actor_tg_id: int | None = None,
                 target: str | None = None,
                 client_id: int | None = None,
                 project_id: int | None = None,
                 detail: dict[str, Any] | None = None) -> None:
    try:
        async with pool().acquire() as conn:
            await conn.execute(
                "INSERT INTO audit_log (tenant_id, actor_kind, actor_id, actor_tg_id, "
                "action, client_id, project_id, target, detail, high_risk) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                tenant_id, actor_kind, actor_id, actor_tg_id, action,
                client_id or None, project_id or None, target,
                json.dumps(detail or {}, ensure_ascii=False),
                action in HIGH_RISK)
    except Exception:
        # Действие уже совершено, откатывать поздно. Молчать нельзя: пропажа аудита
        # сама по себе инцидент.
        log.exception("не удалось записать аудит: теннант %s, действие %s",
                      tenant_id, action)


async def recent(tenant_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Последние записи теннанта — для экрана в приложении."""
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT occurred_at, actor_kind, actor_id, action, target, detail, high_risk "
            "FROM audit_log WHERE tenant_id = $1 ORDER BY occurred_at DESC LIMIT $2",
            tenant_id, limit)
    return [dict(r) for r in rows]
