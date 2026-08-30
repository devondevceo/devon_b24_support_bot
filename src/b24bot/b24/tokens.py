"""Хранилище и обновление per-user токенов Битрикс24.

Инвариант И-5. Тут собраны четыре вещи, каждая из которых по отдельности выглядит
мелочью, а вместе они и есть разница между работающей привязкой и привязкой,
которая разваливается на проде невоспроизводимо:

1. Только pg_advisory_xact_lock. Сессионный pg_advisory_lock с finally: unlock в asyncio
   ломается: между взятием лока и входом в try возможна отмена задачи, лок остаётся
   висеть на соединении, соединение уходит в пул, и все последующие обновления этого
   токена упираются в lock_timeout. Транзакционный лок снимается на COMMIT/ROLLBACK,
   отменить его между «взял» и «отпустил» невозможно.
2. Под локом токен ПЕРЕЧИТЫВАЕТСЯ: пока мы ждали, его мог обновить кто-то другой.
3. Ответ с error не записывается вообще. Типовой баг чужих SDK — вычистить error
   и сохранить мусор как валидные данные; приложение потом «тихо» не работает.
4. UPDATE ... WHERE token_version = $v RETURNING: ноль строк означает, что refresh_token
   мы уже сожгли, а новую пару не сохранили. Это инцидент и needs_reauth, а не
   молчаливый возврат старого токена.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from b24bot.b24 import oauth
from b24bot.core.config import get_settings
from b24bot.crypto import box

log = logging.getLogger(__name__)

REFRESH_MARGIN = timedelta(minutes=5)
LOCK_TIMEOUT_MS = 15_000


class NeedsReauth(Exception):
    """Привязка мертва: пользователь должен снова открыть приложение в Битриксе."""

    def __init__(self, tenant_id: int, b24_user_id: int, reason: str) -> None:
        self.tenant_id, self.b24_user_id, self.reason = tenant_id, b24_user_id, reason
        super().__init__(f"needs_reauth t={tenant_id} u={b24_user_id}: {reason}")


class TokenRaceLost(Exception):
    """Не удалось взять лок за отведённое время. Fail-closed, без обхода лока."""


@dataclass(frozen=True)
class TokenRow:
    tenant_id: int
    b24_user_id: int
    access_token: str
    refresh_token: str
    expires_at: datetime
    token_version: int
    state: str
    authorized_tg_user_id: int | None


def _lock_key(tenant_id: int, b24_user_id: int) -> int:
    raw = f"b24token:{tenant_id}:{b24_user_id}".encode()
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)


async def _note_portal_domain(conn: asyncpg.Connection, tenant_id: int,
                              payload: dict[str, Any]) -> None:
    """Портал переименовали — узнать об этом можно только отсюда.

    `member_id` при переименовании не меняется, а `tenants.b24_domain` до сих пор
    обновлялся только при переустановке: все вызовы ломались до неё. Ответ обмена
    токенов — доверенный источник домена, совместимый с И-4: он приходит по TLS
    с oauth-хоста из allowlist, а не из входящего запроса. Сверх того домен
    проверяется на суффикс Битрикс24, а `member_id` ответа — на принадлежность
    именно этому теннанту: чужой ответ ничего не перепишет.
    """
    from b24bot.core.config import is_trusted_portal_domain

    new_domain = str(payload.get("domain") or "").strip().lower()
    member_id = str(payload.get("member_id") or "")
    if not new_domain or not member_id or not is_trusted_portal_domain(new_domain):
        return
    changed = await conn.fetchval(
        "UPDATE tenants SET b24_domain = $2, updated_at = now() "
        "WHERE id = $1 AND b24_member_id = $3 AND b24_domain <> $2 RETURNING id",
        tenant_id, new_domain, member_id)
    if changed is not None:
        log.warning("портал теннанта %s переехал на домен %s (из ответа "
                    "oauth-сервера)", tenant_id, new_domain)


def _now() -> datetime:
    return datetime.now(UTC)


class TokenStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ чтение
    async def _load(self, conn: asyncpg.Connection, tenant_id: int,
                    b24_user_id: int) -> TokenRow | None:
        row = await conn.fetchrow(
            """
            SELECT tenant_id, b24_user_id, access_token, refresh_token, expires_at,
                   token_version, state, authorized_tg_user_id
            FROM b24_user_tokens WHERE tenant_id = $1 AND b24_user_id = $2
            """, tenant_id, b24_user_id)
        if row is None:
            return None
        ad_a = box.aad("b24_user_tokens", "access_token", tenant_id, b24_user_id)
        ad_r = box.aad("b24_user_tokens", "refresh_token", tenant_id, b24_user_id)
        return TokenRow(
            tenant_id=row["tenant_id"], b24_user_id=row["b24_user_id"],
            access_token=box.decrypt(row["access_token"], ad_a),
            refresh_token=box.decrypt(row["refresh_token"], ad_r),
            expires_at=row["expires_at"], token_version=row["token_version"],
            state=row["state"], authorized_tg_user_id=row["authorized_tg_user_id"],
        )

    async def get_access_token(self, tenant_id: int, b24_user_id: int, *,
                               actor_tg_user_id: int | None = None) -> str:
        """Живой access_token. Обновляет при необходимости.

        actor_tg_user_id обязателен для действий по инициативе человека: токен
        отдаётся только тому, кто его авторизовал. Без этой проверки привязка по
        e-mail открывала доступ к чужому токену (docs/40-security.md §2).
        """
        async with self._pool.acquire() as conn:
            row = await self._load(conn, tenant_id, b24_user_id)

        if row is None:
            raise NeedsReauth(tenant_id, b24_user_id, "токена нет")
        if row.state != "active":
            raise NeedsReauth(tenant_id, b24_user_id, f"состояние {row.state}")
        if actor_tg_user_id is not None and row.authorized_tg_user_id != actor_tg_user_id:
            raise NeedsReauth(tenant_id, b24_user_id,
                              "токен авторизован другим пользователем Telegram")

        if row.expires_at - _now() > REFRESH_MARGIN:
            return row.access_token
        return await self.refresh(tenant_id, b24_user_id)

    # -------------------------------------------------------------- обновление
    async def refresh(self, tenant_id: int, b24_user_id: int) -> str:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
            try:
                await conn.execute("SELECT pg_advisory_xact_lock($1)",
                                   _lock_key(tenant_id, b24_user_id))
            except asyncpg.LockNotAvailableError as exc:
                # Fail-closed. Никакого «пойдём обновлять без лока».
                raise TokenRaceLost(
                    f"лок занят: t={tenant_id} u={b24_user_id}") from exc

            # 2. Перечитать: пока ждали лок, токен мог обновить другой процесс.
            row = await self._load(conn, tenant_id, b24_user_id)
            if row is None:
                raise NeedsReauth(tenant_id, b24_user_id, "токена нет")
            if row.state != "active":
                raise NeedsReauth(tenant_id, b24_user_id, f"состояние {row.state}")
            if row.expires_at - _now() > REFRESH_MARGIN:
                return row.access_token

            payload = await self._exchange(row.refresh_token)

            # 3. Ответ с ошибкой не записываем НИЧЕМ.
            if not payload or payload.get("error"):
                reason = str(payload.get("error") if payload else "пустой ответ")
                await conn.execute(
                    "UPDATE b24_user_tokens SET state = 'needs_reauth' "
                    "WHERE tenant_id = $1 AND b24_user_id = $2",
                    tenant_id, b24_user_id)
                log.error("обновление токена отклонено порталом: t=%s u=%s reason=%s",
                          tenant_id, b24_user_id, reason)
                raise NeedsReauth(tenant_id, b24_user_id, reason)

            new_access = str(payload["access_token"])
            new_refresh = str(payload["refresh_token"])
            expires_in = int(payload.get("expires_in") or 3600)

            ad_a = box.aad("b24_user_tokens", "access_token", tenant_id, b24_user_id)
            ad_r = box.aad("b24_user_tokens", "refresh_token", tenant_id, b24_user_id)
            enc_a = box.encrypt(new_access, ad_a)
            enc_r = box.encrypt(new_refresh, ad_r)

            # 4. Ноль обновлённых строк = сожгли refresh и не сохранили результат.
            updated = await conn.fetchrow(
                """
                    UPDATE b24_user_tokens
                       SET access_token = $1, refresh_token = $2, enc_kid = $3,
                           expires_at = $4, token_version = token_version + 1,
                           last_refresh_at = now(), state = 'active'
                     WHERE tenant_id = $5 AND b24_user_id = $6 AND token_version = $7
                 RETURNING token_version
                    """,
                enc_a, enc_r, box.kid_of(enc_a),
                _now() + timedelta(seconds=expires_in),
                tenant_id, b24_user_id, row.token_version)

            if updated is None:
                await conn.execute(
                    "UPDATE b24_user_tokens SET state = 'needs_reauth' "
                    "WHERE tenant_id = $1 AND b24_user_id = $2",
                    tenant_id, b24_user_id)
                log.error("ИНЦИДЕНТ: refresh сожжён, новая пара не сохранена: "
                          "t=%s u=%s version=%s", tenant_id, b24_user_id,
                          row.token_version)
                raise NeedsReauth(tenant_id, b24_user_id,
                                  "конкурентная запись, пара токенов потеряна")

            await _note_portal_domain(conn, tenant_id, payload)
            return new_access

    async def _exchange(self, refresh_token: str) -> dict[str, Any] | None:
        """Обмен refresh_token. Хосты только из allowlist (И-4).

        Сам перебор хостов живёт в `b24/oauth.py`: там же обмен `code` при
        привязке из Telegram, и правило «первый осмысленный ответ и есть
        результат» обязано быть у обоих одно.
        """
        s = get_settings()
        return await oauth.post_token({
            "grant_type": "refresh_token",
            "client_id": s.b24_client_id,
            "client_secret": s.b24_client_secret,
            "refresh_token": refresh_token,
        })

    # ------------------------------------------------------------------ запись
    async def store(self, tenant_id: int, b24_user_id: int, access_token: str,
                    refresh_token: str, *, role: str = "user", expires_in: int = 3600,
                    authorized_tg_user_id: int | None = None) -> None:
        ad_a = box.aad("b24_user_tokens", "access_token", tenant_id, b24_user_id)
        ad_r = box.aad("b24_user_tokens", "refresh_token", tenant_id, b24_user_id)
        enc_a, enc_r = box.encrypt(access_token, ad_a), box.encrypt(refresh_token, ad_r)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO b24_user_tokens (tenant_id, b24_user_id, role, access_token,
                    refresh_token, enc_kid, expires_at, state, last_refresh_at,
                    authorized_tg_user_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,'active',now(),$8)
                ON CONFLICT (tenant_id, b24_user_id) DO UPDATE
                  SET access_token = EXCLUDED.access_token,
                      refresh_token = EXCLUDED.refresh_token,
                      enc_kid = EXCLUDED.enc_kid,
                      expires_at = EXCLUDED.expires_at,
                      state = 'active',
                      token_version = b24_user_tokens.token_version + 1,
                      last_refresh_at = now(),
                      authorized_tg_user_id =
                        COALESCE(EXCLUDED.authorized_tg_user_id,
                                 b24_user_tokens.authorized_tg_user_id)
                """,
                tenant_id, b24_user_id, role, enc_a, enc_r, box.kid_of(enc_a),
                _now() + timedelta(seconds=expires_in), authorized_tg_user_id)
