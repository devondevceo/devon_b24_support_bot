"""Привязка точек встраивания приложения.

Правило из docs/50-web-and-b24-app.md: точку без содержимого не регистрируем.
Пустая вкладка в карточке каждой задачи портала хуже, чем её отсутствие.

Сейчас готов один экран — главный. Поэтому привязываем только LEFT_MENU.
SONET_GROUP_DETAIL_TAB и TASK_VIEW_TAB добавятся вместе со своими страницами.

Запуск: docker compose run --rm --no-deps -v /opt/b24sdbot/scripts:/app/scripts api \
            python scripts/bind_placements.py [list|bind|unbind]
"""
from __future__ import annotations

import asyncio
import sys

from b24bot.b24.client import B24Client
from b24bot.b24.limiter import PortalLimiter
from b24bot.b24.tokens import TokenStore
from b24bot.core.config import get_settings
from b24bot.db.pool import close_pool, init_pool, pool

TENANT_ID = 1

READY = [
    ("LEFT_MENU", "Поддержка в Telegram",
     "Настройка интеграции с Telegram: бот, клиенты, проекты, привязки чатов"),
]

# Ждут своих страниц, регистрировать рано:
#   SONET_GROUP_DETAIL_TAB — «этот проект привязан к чатам X, Y» + мастер привязки
#   TASK_VIEW_TAB          — «задача обсуждалась в чате Z» со ссылкой на сообщение
#   CRM_COMPANY_DETAIL_TAB — связь компании с клиентом


async def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else "list"
    await init_pool()
    store = TokenStore(pool())
    async with pool().acquire() as conn:
        domain = await conn.fetchval("SELECT b24_domain FROM tenants WHERE id = $1", TENANT_ID)

    handler = f"{get_settings().public_base_url}/b24/placement"

    async with B24Client(domain, lambda: store.get_access_token(TENANT_ID, 1),
                         PortalLimiter()) as client:
        if what == "bind":
            for code, title, description in READY:
                try:
                    await client.call("placement.bind", {
                        "PLACEMENT": code,
                        "HANDLER": handler,
                        "TITLE": title,
                        "DESCRIPTION": description,
                    })
                    print(f"   {code}: привязано -> {handler}")
                except Exception as exc:
                    print(f"   {code}: ОШИБКА {type(exc).__name__}: {exc}")

        if what == "unbind":
            for code, _, _ in READY:
                try:
                    await client.call("placement.unbind",
                                      {"PLACEMENT": code, "HANDLER": handler})
                    print(f"   {code}: снято")
                except Exception as exc:
                    print(f"   {code}: {type(exc).__name__}")

        bound = await client.call("placement.get", {})
        print(f"\nЗарегистрировано встроек: {len(bound) if bound else 0}")
        for b in (bound or []):
            print(f"   {b.get('placement')}  «{b.get('title')}»  -> {b.get('handler')}")

    await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
