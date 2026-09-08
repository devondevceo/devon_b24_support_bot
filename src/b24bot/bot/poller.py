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
import time

from b24bot.bot import commands, dispatch
from b24bot.core import heartbeat
from b24bot.crypto import box
from b24bot.db.pool import pool, system_scope, tenant_scope
from b24bot.tg import api as tg

log = logging.getLogger(__name__)

POLL_TIMEOUT = 25          # сколько Telegram держит соединение, секунд
IDLE_SLEEP = 1.0           # пауза после пустого ответа
ERROR_SLEEP = 5.0          # пауза после ошибки
REFRESH_BOTS_EVERY = 30.0  # как часто перечитываем список ботов

# Жёсткий предел на один заход в getUpdates. HTTP-клиенту таймаут уже задан
# (`tg.http_timeout_for`), но 08.09.2026 на боевом сервере он не сработал:
# запрос завис навсегда, поллер простоял 11 дней, Telegram копил апдейты, а
# контейнер всё это время числился `healthy`. Библиотечный таймаут — обещание
# библиотеки; этот — наше, и обойти его нечем.
POLL_HARD_LIMIT = POLL_TIMEOUT + 20.0
# Сколько цикл может не завершать оборот, прежде чем считать его зависшим.
# Оборот — это getUpdates плюс обработка; при исправной сети он занимает секунды,
# при неисправной — упирается в POLL_HARD_LIMIT и ERROR_SLEEP.
STALL_AFTER = POLL_HARD_LIMIT * 3

# Отступление на короткий опрос. На боевом хосте 08.09.2026 нашлось, что через
# этот SOCKS5-прокси удержание соединения на 25 секунд не доживает до ответа:
# длинный запрос стабильно умирает ReadTimeout, а такой же запрос с нулевым
# ожиданием возвращает апдейты за доли секунды. Бот в итоге не забирал сообщения
# одиннадцать дней, и снаружи это выглядело как «перестал создавать задачи».
#
# Поэтому опрос подстраивается под сеть: несколько таймаутов подряд — переходим
# на короткие запросы (Telegram их разрешает; цена — лишний трафик и задержка
# в пару секунд), а раз в четверть часа пробуем длинный снова. Починят прокси
# или сменят — бот вернётся к длинному опросу сам, без правки кода.
FALLBACK_AFTER = 3         # столько таймаутов подряд означают «длинный не живёт»
SHORT_POLL_SLEEP = 2.0     # пауза между короткими запросами
RETRY_LONG_EVERY = 900.0   # как часто пробовать вернуться к длинному опросу


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
        # Время последнего ЗАВЕРШЁННОГО оборота цикла. Пульс процесса считается
        # по нему, а не по обороту супервизора: супервизор жив всегда, а вопрос,
        # на который отвечает healthcheck, — «идут ли апдейты».
        self._polled_at = time.monotonic()
        # Опрос начинает с длинного и отступает на короткий, только увидев,
        # что длинный на этой сети не доживает до ответа.
        self._short_poll = False
        self._timeouts = 0
        self._long_retry_at = time.monotonic() + RETRY_LONG_EVERY

    def stop(self) -> None:
        self._stop.set()

    def idle_for(self) -> float:
        """Сколько секунд цикл не завершал оборот."""
        return time.monotonic() - self._polled_at

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
            wait = self._poll_timeout()
            limit = wait + (POLL_HARD_LIMIT - POLL_TIMEOUT)
            try:
                # Свой предел поверх клиентского: библиотечный таймаут однажды
                # уже не сработал, и цена этого — молчащий бот, неотличимый
                # снаружи от исправного.
                async with asyncio.timeout(limit):
                    updates = await tg.call(self._token, "getUpdates", {
                        "offset": self._offset + 1,
                        "timeout": wait,
                        "allowed_updates": tg.ALLOWED_UPDATES,
                    })
            except tg.TelegramInvalidToken as exc:
                log.error("токен бота @%s отвергнут (%s) — поллер остановлен",
                          self.username, exc.description)
                await self._mark_error(f"токен отвергнут: {exc.description}")
                return
            except TimeoutError:
                log.warning("getUpdates @%s не ответил за %.0f с — обрываю заход",
                            self.username, limit)
                self._on_timeout()
                await asyncio.sleep(ERROR_SLEEP)
                continue
            except tg.TelegramError as exc:
                log.warning("getUpdates @%s: %s", self.username, exc)
                if exc.code == 0 and "timeout" in exc.description.lower():
                    self._on_timeout()
                else:
                    self._polled_at = time.monotonic()
                await asyncio.sleep(ERROR_SLEEP)
                continue

            self._on_success()
            if not updates:
                await asyncio.sleep(SHORT_POLL_SLEEP if self._short_poll else IDLE_SLEEP)
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
            self._polled_at = time.monotonic()

    # ------------------------------------------------------- подстройка опроса
    def _poll_timeout(self) -> float:
        """Сколько просить Telegram держать соединение.

        Возврат к длинному опросу пробуется по времени, а не по числу удач:
        короткий опрос успешен всегда, и по удачам мы не вернулись бы никогда.
        """
        if self._short_poll and time.monotonic() >= self._long_retry_at:
            self._short_poll = False
            log.info("пробую вернуться к длинному опросу @%s", self.username)
        return 0.0 if self._short_poll else float(POLL_TIMEOUT)

    def _on_timeout(self) -> None:
        """Заход не дожил до ответа. Оборот засчитан: цикл жив, сеть — нет."""
        self._polled_at = time.monotonic()
        self._timeouts += 1
        if not self._short_poll and self._timeouts >= FALLBACK_AFTER:
            self._short_poll = True
            self._long_retry_at = time.monotonic() + RETRY_LONG_EVERY
            log.warning(
                "длинный опрос @%s не доживает до ответа (%d таймаута подряд) — "
                "перехожу на короткие запросы", self.username, self._timeouts)

    def _on_success(self) -> None:
        self._polled_at = time.monotonic()
        if self._timeouts and not self._short_poll:
            log.info("длинный опрос @%s снова работает", self.username)
        self._timeouts = 0
        if self._short_poll:
            # Короткий опрос работает — следующую попытку длинного отложим,
            # чтобы не дёргать сеть впустую каждый оборот.
            self._long_retry_at = max(self._long_retry_at,
                                      time.monotonic() + SHORT_POLL_SLEEP)

    async def _save_offset(self) -> None:
        with tenant_scope(self.tenant_id):
            async with pool().acquire() as conn:
                await conn.execute(
                    "UPDATE tg_bots SET update_offset = $2, last_check_at = now(), "
                    "status = 'active', last_error = NULL WHERE id = $1",
                    self.bot_ref, self._offset)

    async def _mark_error(self, text: str) -> None:
        with tenant_scope(self.tenant_id):
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
        # Реестр ботов кросс-теннантен по построению — системный скоуп (RLS).
        with system_scope():
            async with pool().acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT b.id, b.tenant_id, b.bot_id, b.username, b.token,
                           b.update_offset
                      FROM tg_bots b JOIN tenants t ON t.id = b.tenant_id
                     WHERE b.mode = 'polling' AND b.status IN ('active','pending')
                       AND t.status = 'active'
                    """)

        alive = {r["id"] for r in rows}
        for bot_ref in list(self._tasks):
            task = self._tasks[bot_ref]
            # Зависший цикл — не то же самое, что упавший: задача не `done()`,
            # исключения нет, снаружи всё выглядит работающим. Проверяем по
            # времени последнего оборота, иначе такой цикл живёт вечно и молчит.
            stalled = self._pollers[bot_ref].idle_for() > STALL_AFTER
            if bot_ref not in alive or task.done() or stalled:
                if stalled:
                    log.error("поллер @%s завис: %.0f с без оборота — переподнимаю",
                              self._pollers[bot_ref].username,
                              self._pollers[bot_ref].idle_for())
                self._pollers[bot_ref].stop()
                self._tasks.pop(bot_ref).cancel()
                self._pollers.pop(bot_ref, None)
                if not stalled:
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

    def polling(self) -> bool:
        """Идёт ли опрос. Пустой реестр — это тоже «идёт»: ботов просто нет.

        Пульс раньше ставил супервизор за сам факт своего оборота, и 08.09.2026
        это стоило одиннадцати дней молчания: цикл опроса висел, супервизор
        исправно бился, контейнер числился `healthy`. Здоровье процесса — это
        не «супервизор жив», а «апдейты забираются».
        """
        return all(p.idle_for() <= STALL_AFTER for p in self._pollers.values())

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception("не удалось обновить список ботов")
            else:
                # Пульс — только когда опрос действительно идёт. Зависший цикл
                # `sync()` переподнимет сам; если и это не помогло, healthcheck
                # обязан покраснеть, а не покрывать тишину.
                if self.polling():
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
