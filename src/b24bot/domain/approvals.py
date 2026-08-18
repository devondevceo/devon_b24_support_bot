"""Подтверждение новой задачи ответственным лицом.

Опция на уровне проекта (`task_approval_settings`): включена — после создания
задачи назначенный человек получает в личку с ботом карточку с кнопками
«Подтвердить»/«Отклонить», и его решение двигает задачу на одну из двух заранее
выбранных стадий канбана. Пока решения нет, задача просто существует там, где её
создал сценарий — отдельной стадии «на рассмотрении» не заводится: у задачи и так
есть обычная стадия по умолчанию, а нужны только две развилки решения.

Три вещи, которые стоит понимать до чтения кода:

* **Личка ответственного — не чат клиента.** У бота нет ChatContext для личных
  диалогов (dispatch.py их не регистрирует), поэтому сообщение шлётся напрямую
  `tg.send_message`, а не через `outbox`: тот жёстко привязан к `tg_chats`,
  которых для личных чатов не существует. Если человек ни разу не писал боту
  лично, Telegram отвечает 400/403 — это не ошибка нашего кода, а такая же
  правда, как «письмо на несуществующий адрес»: запрос остаётся `pending` и
  найдётся через /pending, когда человек всё же откроет бота.
* **Гонку двойного клика закрывает не advisory-lock, как в b24/tokens.py, а сам
  атомарный `UPDATE ... WHERE status='pending' RETURNING`.** В tokens.py лок
  нужен потому, что два конкурентных вызова могли бы одновременно дёрнуть OAuth
  refresh одним и тем же refresh_token — внешний вызов ДО записи. Здесь внешний
  вызов (Битрикс) происходит уже ПОСЛЕ того, как строка необратимо перестала
  быть pending, поэтому второй клик физически не может дойти до мутации портала:
  он видит ноль обновлённых строк и получает `already_done`.
* **Строка снимает копию, а не ссылается на текущие настройки.** Если админ
  поменяет стадии или ответственного после отправки запроса, уже отправленный
  человеку выбор не должен подмениться задним числом.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from b24bot.b24 import errors
from b24bot.b24.tokens import NeedsReauth
from b24bot.bot import keyboards
from b24bot.core.text import esc_html
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.domain import access, audit
from b24bot.domain import tasks as task_service
from b24bot.domain.context import ProjectRef, issue_token
from b24bot.tg import api as tg

log = logging.getLogger(__name__)

Decision = Literal["confirm", "reject"]
Outcome = Literal["confirmed", "rejected", "already_done", "needs_reauth",
                  "b24_error", "forbidden", "not_found"]


@dataclass
class Settings:
    project_id: int
    enabled: bool
    responsible_user_id: int | None
    confirm_stage_id: int | None
    confirm_stage_title: str
    reject_stage_id: int | None
    reject_stage_title: str


@dataclass
class PendingItem:
    id: int
    project_id: int
    project_name: str
    client_name: str
    b24_task_id: int
    task_title: str
    requested_at: datetime


@dataclass
class VoteResult:
    outcome: Outcome
    task_id: int = 0
    title: str = ""
    stage_title: str = ""


# -------------------------------------------------------------------- настройки
async def get_settings(tenant_id: int, project_id: int) -> Settings | None:
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT project_id, enabled, responsible_user_id, confirm_stage_id, "
            "confirm_stage_title, reject_stage_id, reject_stage_title "
            "FROM task_approval_settings WHERE tenant_id = $1 AND project_id = $2",
            tenant_id, project_id)
    if row is None:
        return None
    return Settings(
        project_id=int(row["project_id"]), enabled=bool(row["enabled"]),
        responsible_user_id=row["responsible_user_id"],
        confirm_stage_id=row["confirm_stage_id"],
        confirm_stage_title=row["confirm_stage_title"],
        reject_stage_id=row["reject_stage_id"],
        reject_stage_title=row["reject_stage_title"])


async def settings_for_projects(tenant_id: int, project_ids: list[int]
                                ) -> dict[int, Settings]:
    """Настройки сразу нескольких проектов — для экрана со списком проектов."""
    if not project_ids:
        return {}
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT project_id, enabled, responsible_user_id, confirm_stage_id, "
            "confirm_stage_title, reject_stage_id, reject_stage_title "
            "FROM task_approval_settings WHERE tenant_id = $1 "
            "AND project_id = ANY($2::bigint[])", tenant_id, project_ids)
    return {int(r["project_id"]): Settings(
                project_id=int(r["project_id"]), enabled=bool(r["enabled"]),
                responsible_user_id=r["responsible_user_id"],
                confirm_stage_id=r["confirm_stage_id"],
                confirm_stage_title=r["confirm_stage_title"],
                reject_stage_id=r["reject_stage_id"],
                reject_stage_title=r["reject_stage_title"])
            for r in rows}


async def upsert_settings(tenant_id: int, project_id: int, *, enabled: bool,
                          responsible_user_id: int | None,
                          confirm_stage_id: int | None, confirm_stage_title: str,
                          reject_stage_id: int | None, reject_stage_title: str) -> None:
    async with pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_approval_settings (tenant_id, project_id, enabled,
                responsible_user_id, confirm_stage_id, confirm_stage_title,
                reject_stage_id, reject_stage_title, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now())
            ON CONFLICT (tenant_id, project_id) DO UPDATE SET
                enabled = EXCLUDED.enabled,
                responsible_user_id = EXCLUDED.responsible_user_id,
                confirm_stage_id = EXCLUDED.confirm_stage_id,
                confirm_stage_title = EXCLUDED.confirm_stage_title,
                reject_stage_id = EXCLUDED.reject_stage_id,
                reject_stage_title = EXCLUDED.reject_stage_title,
                updated_at = now()
            """, tenant_id, project_id, enabled, responsible_user_id,
            confirm_stage_id, confirm_stage_title or "",
            reject_stage_id, reject_stage_title or "")


async def user_id_of(tg_user_id: int) -> int | None:
    async with pool().acquire() as conn:
        value = await conn.fetchval("SELECT id FROM users WHERE tg_user_id = $1", tg_user_id)
    return int(value) if value is not None else None


# --------------------------------------------------------------------- запрос
async def on_task_created(tenant_id: int, project: ProjectRef, task: dict[str, Any]) -> None:
    """Хук из `task_create.create()`, только что созданная задача.

    Никогда не поднимает исключение: задача в Битриксе уже создана и ответ
    человеку в чате не имеет права пропасть из-за того, что запрос на
    подтверждение не отправился (тот же принцип, что у `audit.record`).
    """
    try:
        settings = await get_settings(tenant_id, project.id)
        if settings is None or not settings.enabled:
            return
        if not (settings.responsible_user_id and settings.confirm_stage_id
                and settings.reject_stage_id):
            log.warning("подтверждение включено для проекта %s, но настройки "
                       "неполны — запрос не отправлен", project.id)
            return

        task_id = int(task["id"])
        title = str(task.get("title") or "")
        async with pool().acquire() as conn:
            approval_id = await conn.fetchval(
                """
                INSERT INTO task_approvals (tenant_id, project_id, b24_task_id, task_title,
                    responsible_user_id, confirm_stage_id, confirm_stage_title,
                    reject_stage_id, reject_stage_title, status, requested_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'pending',now())
                ON CONFLICT (tenant_id, b24_task_id) WHERE status = 'pending'
                  DO NOTHING
                RETURNING id
                """,
                tenant_id, project.id, task_id, title, settings.responsible_user_id,
                settings.confirm_stage_id, settings.confirm_stage_title,
                settings.reject_stage_id, settings.reject_stage_title)
        if approval_id is None:
            return

        await _notify(tenant_id, int(approval_id), project, task_id, title,
                     int(settings.responsible_user_id))
    except Exception:
        log.exception("не удалось запросить подтверждение задачи: теннант %s, "
                      "проект %s, задача %s", tenant_id, project.id, task.get("id"))


async def _notify(tenant_id: int, approval_id: int, project: ProjectRef, task_id: int,
                  title: str, responsible_user_id: int) -> None:
    async with pool().acquire() as conn:
        person = await conn.fetchrow(
            "SELECT u.tg_user_id, m.b24_user_id FROM users u "
            "LEFT JOIN tenant_members m ON m.tenant_id = $2 AND m.user_id = u.id "
            "WHERE u.id = $1", responsible_user_id, tenant_id)
        bot_row = await conn.fetchrow(
            "SELECT id, bot_id, token FROM tg_bots WHERE tenant_id = $1", tenant_id)
        domain = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1",
                                     tenant_id)
    if person is None or person["tg_user_id"] is None or bot_row is None:
        log.warning("запрос на подтверждение %s не отправлен: нет привязки Telegram "
                   "у ответственного или бот не подключён", approval_id)
        return

    token = box.decrypt(bot_row["token"],
                        box.aad("tg_bots", "token", tenant_id, bot_row["bot_id"]))
    tg_user_id = int(person["tg_user_id"])

    confirm = await issue_token("task_approval", tenant_id=tenant_id,
                                owner_tg_id=tg_user_id,
                                payload={"approval_id": approval_id, "decision": "confirm"},
                                ttl=timedelta(days=30))
    reject = await issue_token("task_approval", tenant_id=tenant_id,
                               owner_tg_id=tg_user_id,
                               payload={"approval_id": approval_id, "decision": "reject"},
                               ttl=timedelta(days=30))
    # Номер — ссылка на задачу: решение принимают, посмотрев её целиком.
    from b24bot.bot.views import task_ref
    ref = task_ref(task_id, domain=str(domain or ""),
                   b24_user_id=person["b24_user_id"])
    text = (f"🙋 <b>Требуется подтверждение</b>\n\n"
           f"<b>{ref} · {esc_html(title)}</b>\n"
           f"Клиент: {esc_html(project.client_name)} · Проект: {esc_html(project.name)}")
    markup = keyboards.inline([[
        keyboards.cb("av", confirm, "✅ Подтвердить"),
        keyboards.cb("av", reject, "❌ Отклонить"),
    ]])
    try:
        await tg.send_message(token, tg_user_id, text, reply_markup=markup)
    except tg.TelegramError as exc:
        # 400/403 — человек ни разу не писал боту лично. Запрос остаётся pending
        # и будет виден через /pending, когда он всё же откроет диалог.
        log.info("запрос на подтверждение %s не доставлен в личку %s: %s",
                approval_id, tg_user_id, exc)


async def pending_for(tenant_id: int, responsible_user_id: int, *,
                      project_ids: list[int] | None = None,
                      limit: int = 30) -> tuple[list[PendingItem], int]:
    """Задачи, ожидающие решения этого человека. `total` — для честного «показано N из M»."""
    async with pool().acquire() as conn:
        total = await conn.fetchval(
            "SELECT count(*) FROM task_approvals WHERE tenant_id = $1 "
            "AND responsible_user_id = $2 AND status = 'pending' "
            "AND ($3::bigint[] IS NULL OR project_id = ANY($3::bigint[]))",
            tenant_id, responsible_user_id, project_ids)
        rows = await conn.fetch(
            """
            SELECT a.id, a.project_id, p.name AS project_name, cl.name AS client_name,
                   a.b24_task_id, a.task_title, a.requested_at
              FROM task_approvals a
              JOIN projects p ON p.id = a.project_id
              JOIN clients cl ON cl.id = p.client_id
             WHERE a.tenant_id = $1 AND a.responsible_user_id = $2 AND a.status = 'pending'
               AND ($3::bigint[] IS NULL OR a.project_id = ANY($3::bigint[]))
             ORDER BY a.requested_at
             LIMIT $4
            """, tenant_id, responsible_user_id, project_ids, limit)
    items = [PendingItem(id=int(r["id"]), project_id=int(r["project_id"]),
                         project_name=r["project_name"], client_name=r["client_name"],
                         b24_task_id=int(r["b24_task_id"]), task_title=r["task_title"],
                         requested_at=r["requested_at"]) for r in rows]
    return items, int(total or 0)


# --------------------------------------------------------------------- решение
async def resolve(tenant_id: int, approval_id: int, decision: Decision,
                  actor_tg_user_id: int, *, source: str = "bot") -> VoteResult:
    """Обработать нажатие «Подтвердить»/«Отклонить».

    Гонку двойного клика (или повтор с двух устройств) закрывает атомарный
    `UPDATE ... WHERE status='pending' RETURNING` — см. докстринг модуля.
    Если Битрикс отказал ПОСЛЕ этого шага, строка откатывается обратно в
    pending: иначе задача осталась бы «подтверждённой» у нас и нетронутой на
    портале, а повторно решить уже нельзя было бы — токен кнопки одноразовый.
    """
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT a.id, a.project_id, a.b24_task_id, a.task_title, a.status,
                   a.confirm_stage_id, a.confirm_stage_title,
                   a.reject_stage_id, a.reject_stage_title,
                   u.tg_user_id AS responsible_tg_id, m.b24_user_id AS responsible_b24_id
              FROM task_approvals a
              JOIN users u ON u.id = a.responsible_user_id
              LEFT JOIN tenant_members m ON m.tenant_id = a.tenant_id
                                         AND m.user_id = a.responsible_user_id
             WHERE a.id = $1 AND a.tenant_id = $2
            """, approval_id, tenant_id)
        if row is None:
            return VoteResult("not_found")
        if int(row["responsible_tg_id"] or 0) != actor_tg_user_id:
            log.warning("чужое нажатие кнопки подтверждения: approval=%s, ждали tg=%s, "
                       "нажал tg=%s", approval_id, row["responsible_tg_id"],
                       actor_tg_user_id)
            return VoteResult("forbidden")
        if row["status"] != "pending":
            return VoteResult("already_done", task_id=row["b24_task_id"],
                             title=row["task_title"])

        target_stage_id = (row["confirm_stage_id"] if decision == "confirm"
                           else row["reject_stage_id"])
        target_stage_title = (row["confirm_stage_title"] if decision == "confirm"
                              else row["reject_stage_title"])
        new_status: Outcome = "confirmed" if decision == "confirm" else "rejected"

        updated = await conn.fetchrow(
            "UPDATE task_approvals SET status = $1, resolved_at = now() "
            "WHERE id = $2 AND status = 'pending' RETURNING id",
            new_status, approval_id)
    if updated is None:
        return VoteResult("already_done", task_id=row["b24_task_id"], title=row["task_title"])

    b24_user_id = row["responsible_b24_id"]
    if b24_user_id is None:
        await _rollback(approval_id)
        return VoteResult("needs_reauth", task_id=row["b24_task_id"], title=row["task_title"])

    try:
        client = await access.client_for_user(tenant_id, int(b24_user_id),
                                              actor_tg_user_id=actor_tg_user_id)
        async with client:
            await task_service.apply_patch(
                client, tenant_id, int(row["b24_task_id"]), {"stage_id": target_stage_id},
                actor_b24_user_id=int(b24_user_id))
    except NeedsReauth:
        await _rollback(approval_id)
        return VoteResult("needs_reauth", task_id=row["b24_task_id"], title=row["task_title"])
    except errors.B24Error as exc:
        await _rollback(approval_id)
        log.error("подтверждение задачи %s не применилось в Битриксе: approval=%s, %s",
                 row["b24_task_id"], approval_id, exc)
        return VoteResult("b24_error", task_id=row["b24_task_id"], title=row["task_title"])

    await audit.record(tenant_id, f"task.approval.{decision}", actor_id=int(b24_user_id),
                       actor_tg_id=actor_tg_user_id, target=f"task:{row['b24_task_id']}",
                       project_id=row["project_id"],
                       detail={"approval_id": approval_id, "stage": target_stage_title,
                               "source": source})
    return VoteResult(new_status, task_id=row["b24_task_id"], title=row["task_title"],
                      stage_title=target_stage_title)


async def _rollback(approval_id: int) -> None:
    """Мутация Битрикса не удалась — вернуть запрос в pending, чтобы его можно
    было решить повторно (например, после /link заново авторизовать доступ)."""
    async with pool().acquire() as conn:
        await conn.execute(
            "UPDATE task_approvals SET status = 'pending', resolved_at = NULL "
            "WHERE id = $1", approval_id)
