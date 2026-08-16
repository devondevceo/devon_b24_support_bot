"""Приём апдейтов через long polling.

Почему polling, а не вебхук. Проверено на боевом сервере 16.08.2026:
  * `api.telegram.org` с сервера недоступен напрямую — таймаут;
  * серверы Telegram, в свою очередь, НЕ МОГУТ достучаться до нашего домена:
    `getWebhookInfo` стабильно отдаёт `Connection timed out`, а в логах приложения
    ноль запросов на `/tg/`.

То есть фильтрация двусторонняя, и вебхук на этом хосте нежизнеспособен. Исходящие
вызовы идут через SOCKS5 (тот же прокси, что у mclick), приём — long polling.
Обработчик вебхука в коде остаётся: он заработает без единой правки, если сервис
переедет на хост с прямым доступом.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from b24bot.bot import commands, dispatch
from b24bot.core import heartbeat
from b24bot.crypto import box
from b24bot.db.pool import pool
from b24bot.tg import api as tg

log = logging.getLogger(__name__)

POLL_TIMEOUT = 25          # сколько Telegram держит соединение, секунд
IDLE_SLEEP = 1.0           # пауза после пустого ответа
ERROR_SLEEP = 5.0          # пауза после ошибки
REFRESH_BOTS_EVERY = 30.0  # как часто перечитываем список ботов


class BotPoller:
    """Один цикл на одного бота теннанта."""

    def __init__(self, bot_ref: int, tenant_id: int, bot_id: int, username: str,
                 token: str, offset: int) -> None:
        self.bot_ref = bot_ref
        self.tenant_id = tenant_id
        self.bot_id = bot_id
        self.username = username
        self._token = token
        self._offset = offset
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _publish_commands(self) -> None:
        """Меню слеш-команд. Своё на личку, на группы и на админов группы.

        Ставится при каждом старте: это единственный момент, когда мы точно знаем,
        что токен жив. Недоступность Telegram здесь не должна мешать поллеру —
        без меню бот работает, команды всё равно набираются руками.
        """
        for scope in commands.scopes():
            try:
                await tg.set_my_commands(self._token, commands.for_scope(scope),
                                         scope=scope)
            except tg.TelegramError as exc:
                log.warning("меню команд (%s) не установлено для @%s: %s",
                            scope, self.username, exc)
                return
        log.info("меню команд обновлено: @%s", self.username)

    async def run(self) -> None:
        log.info("поллер запущен: @%s (теннант %s)", self.username, self.tenant_id)
        # Вебхук и getUpdates взаимоисключающи: Telegram отдаст 409, пока висит вебхук.
        with contextlib.suppress(tg.TelegramError):
            await tg.delete_webhook(self._token)
        await self._publish_commands()

        while not self._stop.is_set():
            try:
                updates = await tg.call(self._token, "getUpdates", {
                    "offset": self._offset + 1,
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": tg.ALLOWED_UPDATES,
                })
            except tg.TelegramInvalidToken as exc:
                log.error("токен бота @%s отвергнут (%s) — поллер остановлен",
                          self.username, exc.description)
                await self._mark_error(f"токен отвергнут: {exc.description}")
                return
            except tg.TelegramError as exc:
                log.warning("getUpdates @%s: %s", self.username, exc)
                await asyncio.sleep(ERROR_SLEEP)
                continue

            if not updates:
                await asyncio.sleep(IDLE_SLEEP)
                continue

            for update in updates:
                update_id = int(update.get("update_id") or 0)
                try:
                    await dispatch.handle(self.bot_ref, self.tenant_id, update)
                    await dispatch.route(self.bot_ref, update)
                except Exception:
                    # Апдейт, который валит обработку, не должен зациклить поллер:
                    # сдвигаем offset и идём дальше, разбираемся по логам.
                    log.exception("ошибка обработки апдейта %s", update_id)
                self._offset = max(self._offset, update_id)

            await self._save_offset()

    async def _save_offset(self) -> None:
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE tg_bots SET update_offset = $2, last_check_at = now(), "
                "status = 'active', last_error = NULL WHERE id = $1",
                self.bot_ref, self._offset)

    async def _mark_error(self, text: str) -> None:
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE tg_bots SET status = 'error', last_error = $2, "
                "last_check_at = now() WHERE id = $1", self.bot_ref, text[:500])


def _report_death(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("поллер упал с исключением", exc_info=exc)


class PollerRegistry:
    """Следит за составом ботов: подключили нового — поднимаем цикл, отключили — гасим."""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._pollers: dict[int, BotPoller] = {}

    async def sync(self) -> None:
        async with pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT b.id, b.tenant_id, b.bot_id, b.username, b.token, b.update_offset
                  FROM tg_bots b JOIN tenants t ON t.id = b.tenant_id
                 WHERE b.mode = 'polling' AND b.status IN ('active','pending')
                   AND t.status = 'active'
                """)

        alive = {r["id"] for r in rows}
        for bot_ref in list(self._tasks):
            if bot_ref not in alive or self._tasks[bot_ref].done():
                self._pollers[bot_ref].stop()
                self._tasks.pop(bot_ref).cancel()
                self._pollers.pop(bot_ref, None)
                log.info("поллер остановлен: bot_ref=%s", bot_ref)

        for r in rows:
            if r["id"] in self._tasks:
                continue
            token = box.decrypt(
                r["token"], box.aad("tg_bots", "token", r["tenant_id"], r["bot_id"]))
            poller = BotPoller(r["id"], r["tenant_id"], r["bot_id"],
                               r["username"], token, int(r["update_offset"] or 0))
            self._pollers[r["id"]] = poller
            task = asyncio.create_task(poller.run())
            # Без этого исключение внутри задачи тонет: цикл просто исчезает, а в
            # логах остаётся только «поллер остановлен» без причины.
            task.add_done_callback(_report_death)
            self._tasks[r["id"]] = task

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception("не удалось обновить список ботов")
            else:
                # Пульс ставит супервизор, а не поллер: поллер может висеть в
                # долгом getUpdates до 25 секунд, и это нормальная работа.
                heartbeat.beat("bot")
            await asyncio.sleep(REFRESH_BOTS_EVERY)


async def main() -> None:
    from b24bot.core.config import get_settings
    from b24bot.core.logging import setup as log_setup
    from b24bot.db.pool import close_pool, init_pool

    s = get_settings()
    log_setup(s.log_level)
    await init_pool()
    log.info("сервис бота запущен, прокси: %s",
             (s.tg_proxy or "нет").split("@")[-1])
    try:
        await PollerRegistry().run_forever()
    finally:
        await close_pool()


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
