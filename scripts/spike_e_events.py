"""SPIKE E: события Битрикс24.

Что выясняем:
  1. Какие события модуля task реально доступны приложению.
  2. Что лежит в теле события: есть ли user_id (кто изменил), есть ли изменённые поля.
  3. Приходит ли ONTASKUPDATE на добавление комментария (это удвоение потока).
  4. Сколько событий даёт одно действие в интерфейсе.
  5. Совпадает ли application_token в событии с тем, что пришёл при установке.

Запуск:  docker compose run --rm --no-deps -v /opt/b24sdbot/scripts:/app/scripts api \
             python scripts/spike_e_events.py [bind|trigger|show|unbind]
"""
from __future__ import annotations

import asyncio
import json
import sys

from b24bot.b24.client import B24Client
from b24bot.b24.limiter import PortalLimiter
from b24bot.b24.tokens import TokenStore
from b24bot.core.config import get_settings
from b24bot.db.pool import close_pool, init_pool, pool

TENANT_ID = 1
B24_USER_ID = 1
GROUP = 33
MARK = "DEVONEVENTPROBE"

WANTED = [
    "ONTASKADD", "ONTASKUPDATE", "ONTASKDELETE",
    "ONTASKCOMMENTADD", "ONTASKCOMMENTUPDATE", "ONTASKCOMMENTDELETE",
    "ONAPPUPDATE", "ONAPPUNINSTALL",
]


def head(t: str) -> None:
    print(f"\n{'=' * 68}\n{t}\n{'=' * 68}")


async def make_client() -> tuple[B24Client, str]:
    store = TokenStore(pool())
    async with pool().acquire() as conn:
        domain = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1", TENANT_ID)

    async def provider() -> str:
        return await store.get_access_token(TENANT_ID, B24_USER_ID)

    return B24Client(domain, provider, PortalLimiter()), domain


async def cmd_bind(client: B24Client) -> None:
    handler = f"{get_settings().public_base_url}/b24/events"
    head(f"Привязка обработчиков на {handler}")

    available = await client.call("events", {})
    names = {str(x).upper() for x in available} if isinstance(available, list) else set()
    print(f"   доступно событий всего: {len(names)}")
    task_events = sorted(n for n in names if "TASK" in n)
    print(f"   из них про задачи ({len(task_events)}): {', '.join(task_events[:20])}")

    for event in WANTED:
        if event not in names:
            print(f"   {event:24} ПРОПУСК — портал такого события не знает")
            continue
        try:
            await client.call("event.bind", {"event": event, "handler": handler})
            print(f"   {event:24} привязан")
        except Exception as exc:
            print(f"   {event:24} ОШИБКА: {type(exc).__name__}: {exc}")

    bound = await client.call("event.get", {})
    print(f"\n   event.get подтверждает подписок: {len(bound) if bound else 0}")
    for b in (bound or []):
        print(f"      {b.get('event'):24} -> {b.get('handler')}")


async def cmd_trigger(client: B24Client) -> None:
    head("Провокация событий на тестовой задаче")

    created = await client.call("tasks.task.add", {"fields": {
        "TITLE": f"{MARK} проверка событий",
        "RESPONSIBLE_ID": 1,
        "GROUP_ID": GROUP,
    }})
    task_id = created["task"]["id"]
    print(f"   1. создана задача #{task_id}  -> ждём ONTASKADD")
    await asyncio.sleep(3)

    await client.call("tasks.task.update", {"taskId": task_id,
                                            "fields": {"TITLE": f"{MARK} переименована"}})
    print("   2. переименована             -> ждём ONTASKUPDATE")
    await asyncio.sleep(3)

    await client.call("tasks.task.start", {"taskId": task_id})
    print("   3. переведена в работу       -> ждём ONTASKUPDATE")
    await asyncio.sleep(3)

    await client.call("task.commentitem.add", {
        "TASKID": task_id,
        "FIELDS": {"POST_MESSAGE": f"комментарий {MARK}"}})
    print("   4. добавлен комментарий      -> ждём ONTASKCOMMENTADD (и, возможно, ONTASKUPDATE)")
    await asyncio.sleep(4)

    check = await client.call("tasks.task.get", {"taskId": task_id, "select": ["ID", "TITLE"]})
    if MARK in check["task"]["title"]:
        await client.call("tasks.task.delete", {"taskId": task_id})
        print(f"   5. задача #{task_id} удалена   -> ждём ONTASKDELETE")
    await asyncio.sleep(4)


async def cmd_show() -> None:
    head("Что реально прилетело на /b24/events")
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT kind, shape, received_at FROM b24_payload_log "
            "WHERE kind LIKE 'event:%' ORDER BY id")
    if not rows:
        print("   событий не пришло. Это сам по себе результат: значит либо подписка "
              "не создалась, либо портал не достучался до обработчика.")
        return

    print(f"   всего событий: {len(rows)}\n")
    seen: set[str] = set()
    for r in rows:
        print(f"   [{r['received_at']:%H:%M:%S}] {r['kind']}")
        if r["kind"] not in seen:
            seen.add(r["kind"])
            print(json.dumps(json.loads(r["shape"]), ensure_ascii=False, indent=6))

    head("Выводы")
    kinds = [r["kind"] for r in rows]
    print(f"   ONTASKUPDATE пришло раз: {kinds.count('event:ONTASKUPDATE')}")
    print(f"   ONTASKCOMMENTADD пришло раз: {kinds.count('event:ONTASKCOMMENTADD')}")
    shapes = [json.loads(r["shape"]) for r in rows]
    has_user = any(any("user_id" in k.lower() for k in s) for s in shapes)
    print(f"   есть ли user_id в теле события: {'ДА' if has_user else 'НЕТ'}")
    keys: set[str] = set()
    for s in shapes:
        keys.update(s.keys())
    print(f"   все встреченные поля: {', '.join(sorted(keys))}")


async def cmd_unbind(client: B24Client) -> None:
    handler = f"{get_settings().public_base_url}/b24/events"
    head("Снятие подписок")
    for event in WANTED:
        try:
            await client.call("event.unbind", {"event": event, "handler": handler})
            print(f"   {event} снят")
        except Exception as exc:
            print(f"   {event}: {type(exc).__name__}")


async def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    await init_pool()
    client, domain = await make_client()
    print(f"портал: {domain}")

    async with client:
        if what in ("bind", "all"):
            await cmd_bind(client)
        if what in ("trigger", "all"):
            await cmd_trigger(client)
        if what in ("show", "all"):
            await cmd_show()
        if what == "unbind":
            await cmd_unbind(client)

    await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
