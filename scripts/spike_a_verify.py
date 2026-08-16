"""SPIKE A: проверка всей цепочки на живом портале.

1. Токен из БД расшифровывается и работает в реальном вызове REST.
2. Обновление токена через oauth.bitrix24.tech проходит с нашими client_id/secret.
3. Пять ПАРАЛЛЕЛЬНЫХ обновлений одного токена дают ровно один обмен с порталом —
   это и есть проверка инварианта И-5 не на моках, а на боевом OAuth.

Запуск внутри контейнера api:
    docker compose run --rm --no-deps api python scripts/spike_a_verify.py
"""
from __future__ import annotations

import asyncio
import sys

from b24bot.b24.client import B24Client
from b24bot.b24.limiter import PortalLimiter
from b24bot.b24.tokens import TokenStore
from b24bot.db.pool import close_pool, init_pool, pool

TENANT_ID = 1
B24_USER_ID = 1


def head(t: str) -> None:
    print(f"\n{'=' * 68}\n{t}\n{'=' * 68}")


async def main() -> int:
    await init_pool()
    store = TokenStore(pool())
    limiter = PortalLimiter()
    failures = 0

    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT b24_domain FROM tenants WHERE id = $1", TENANT_ID)
    domain = row["b24_domain"]

    # ------------------------------------------------------------------ 1
    head("1. Токен из БД работает в живом вызове")
    token = await store.get_access_token(TENANT_ID, B24_USER_ID)
    print(f"   access_token расшифрован, длина {len(token)}")

    async def provider() -> str:
        return await store.get_access_token(TENANT_ID, B24_USER_ID)

    async with B24Client(domain, provider, limiter) as client:
        me = await client.call("user.current")
        print(f"   user.current -> ID={me.get('ID')} {me.get('NAME')} {me.get('LAST_NAME')}")

        groups = await client.call("sonet_group.get", {"ORDER": {"ID": "ASC"}})
        print(f"   sonet_group.get -> групп: {len(groups)}")

        tasks = await client.call("tasks.task.list", {
            "filter": {"GROUP_ID": 33},
            "select": ["ID", "TITLE", "STATUS", "STAGE_ID"],
        })
        items = tasks.get("tasks", []) if isinstance(tasks, dict) else []
        print(f"   tasks.task.list(проект 33) -> задач: {len(items)}")
        for t in items[:4]:
            print(f"      #{t['id']} status={t['status']} stage={t.get('stageId')} "
                  f"{t['title'][:40]}")

        stages = await client.call("task.stages.get", {"entityId": 33})
        print(f"   task.stages.get -> стадии: "
              f"{', '.join(s['TITLE'] for s in stages.values())}")

        print(f"   бюджет портала после серии: {limiter.stats}")

    # ------------------------------------------------------------------ 2
    head("2. Обновление токена через oauth.bitrix24.tech")
    async with pool().acquire() as conn:
        before = await conn.fetchrow(
            "SELECT token_version, expires_at FROM b24_user_tokens "
            "WHERE tenant_id = $1 AND b24_user_id = $2", TENANT_ID, B24_USER_ID)
        # Искусственно помечаем токен протухшим, чтобы вызвать реальный refresh.
        await conn.execute(
            "UPDATE b24_user_tokens SET expires_at = now() - interval '1 minute' "
            "WHERE tenant_id = $1 AND b24_user_id = $2", TENANT_ID, B24_USER_ID)

    new_token = await store.refresh(TENANT_ID, B24_USER_ID)
    async with pool().acquire() as conn:
        after = await conn.fetchrow(
            "SELECT token_version, expires_at, state FROM b24_user_tokens "
            "WHERE tenant_id = $1 AND b24_user_id = $2", TENANT_ID, B24_USER_ID)

    print(f"   token_version: {before['token_version']} -> {after['token_version']}")
    print(f"   state: {after['state']}, живёт до {after['expires_at']}")
    if after["token_version"] != before["token_version"] + 1 or after["state"] != "active":
        print("   ПРОВАЛ: версия или состояние не те")
        failures += 1
    else:
        print("   OK: пара токенов обновлена и сохранена")

    # новый токен обязан работать
    async with B24Client(domain, lambda: _const(new_token), limiter) as client:
        me = await client.call("user.current")
        print(f"   новый токен в деле: user.current -> ID={me.get('ID')}")

    # ------------------------------------------------------------------ 3
    head("3. Пять параллельных обновлений — инвариант И-5")
    async with pool().acquire() as conn:
        v0 = await conn.fetchval(
            "SELECT token_version FROM b24_user_tokens "
            "WHERE tenant_id = $1 AND b24_user_id = $2", TENANT_ID, B24_USER_ID)
        await conn.execute(
            "UPDATE b24_user_tokens SET expires_at = now() - interval '1 minute' "
            "WHERE tenant_id = $1 AND b24_user_id = $2", TENANT_ID, B24_USER_ID)

    results = await asyncio.gather(
        *[store.refresh(TENANT_ID, B24_USER_ID) for _ in range(5)],
        return_exceptions=True)

    ok = [r for r in results if isinstance(r, str)]
    errs = [r for r in results if not isinstance(r, str)]
    async with pool().acquire() as conn:
        v1, state = await conn.fetchrow(
            "SELECT token_version, state FROM b24_user_tokens "
            "WHERE tenant_id = $1 AND b24_user_id = $2", TENANT_ID, B24_USER_ID)

    print(f"   успешных: {len(ok)}, с ошибкой: {len(errs)}")
    for e in errs:
        print(f"      {type(e).__name__}: {e}")
    print(f"   token_version: {v0} -> {v1} (обменов с порталом: {v1 - v0})")
    print(f"   уникальных выданных токенов: {len(set(ok))}")
    print(f"   состояние привязки: {state}")

    if v1 - v0 != 1:
        print("   ПРОВАЛ: обменов должно быть ровно 1 — остальные обязаны были "
              "увидеть свежий токен под локом")
        failures += 1
    elif state != "active":
        print("   ПРОВАЛ: привязка не осталась активной")
        failures += 1
    elif len(ok) != 5:
        print("   ПРОВАЛ: не все вызовы получили токен")
        failures += 1
    else:
        print("   OK: один обмен, пять успехов, привязка жива")

    head("ИТОГ")
    print("   всё сошлось" if failures == 0 else f"   провалов: {failures}")
    await close_pool()
    return 1 if failures else 0


async def _const(v: str) -> str:
    return v


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
