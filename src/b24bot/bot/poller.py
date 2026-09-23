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
import re
import time

from b24bot.bot import commands, dispatch
from b24bot.core import heartbeat
from b24bot.core.config import get_settings
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

# Сколько поллер может не получать ОТВЕТА Telegram, прежде чем считаться глухим.
#
# Оборот цикла и ответ Telegram — разные события. 23.09.2026 бот не отвечал ни на
# одну команду, а зелёным было всё: контейнер `healthy`, `/health` — `ok`, экран в
# Битриксе — «Интеграция работает». Оборотом засчитывался и 409 от Telegram, и отказ
# прокси, и таймаут, то есть цикл, в котором не прошёл ни один `getUpdates`, числился
# живым. Сторож 08.09 ловил зависший цикл, а этот цикл не висел — он честно крутился
# вхолостую.
#
# Предел длиннее отступления на короткий опрос (FALLBACK_AFTER заходов по
# POLL_HARD_LIMIT плюс паузы — около 150 с): сеть, где длинный опрос не живёт, а
# короткий живёт, глухотой не считается — бот в ней работает.
DEAF_AFTER = 300.0
# Как часто отмечать в базе, что Telegram отвечает (`tg_bots.heard_at`). Отметку
# читает экран приложения, где счёт идёт на минуты; чаще — лишняя запись в базу на
# каждый короткий опрос.
HEARD_MARK_EVERY = 60.0

# Логин и пароль прокси в тексте ошибки транспорта. Причина глухоты уходит на экран
# администратора теннанта, а исключение httpx вправе процитировать адрес целиком.
_CREDENTIALS = re.compile(r"(\w+://)[^/\s@]+@")

# Состояние приёма в `tg_bots` (миграция 0021). `status` поллер трогает только в
# одном случае — чужой вебхук, то есть токен у третьих лиц.
_SQL_HEARD = "UPDATE tg_bots SET heard_at = now(), poll_error = NULL WHERE id = $1"
_SQL_DEAF = "UPDATE tg_bots SET poll_error = $2 WHERE id = $1"
_SQL_SUSPEND = ("UPDATE tg_bots SET status = 'suspended', last_error = $2, "
                "last_check_at = now() WHERE id = $1")


def is_webhook_conflict(exc: tg.TelegramError) -> bool:
    """409, который означает вебхук на боте, а не второй экземпляр опроса.

    Telegram отвечает так в двух случаях: на `getUpdates`, пока вебхук стоит, и
    обрывая уже висящий long poll в момент `setWebhook`. Лечится одинаково.
    """
    return exc.code == 409 and "webhook" in exc.description.lower()


def is_own_webhook(url: str) -> bool:
    """Вебхук на наш же адрес приёма — след своей конфигурации, а не чужой."""
    return url.startswith(get_settings().public_base_url.rstrip("/") + "/tg/")


def describe_failure(exc: BaseException) -> str:
    """Почему заход в getUpdates не удался — человеческим языком.

    Текст уходит в `tg_bots.poll_error` и показывается администратору на экране
    приложения, поэтому в нём нет ни токена (его нет и в описаниях Telegram), ни
    логина с паролем прокси.
    """
    if isinstance(exc, TimeoutError):
        return ("Telegram не ответил на запрос новых сообщений за отведённое время — "
                "обычно это прокси, через который сервер ходит в Telegram")
    if isinstance(exc, tg.TelegramError):
        if is_webhook_conflict(exc):
            return "на боте установлен вебхук, и Telegram не отдаёт сообщения опросом (409)"
        if exc.code == 409:
            return ("этого бота опрашивает ещё один процесс с тем же токеном (409): "
                    "сообщения уходят туда")
        if exc.code == 0:
            detail = _CREDENTIALS.sub(r"\1", exc.description)
            return f"нет связи с Telegram через прокси ({detail})"
        return f"Telegram отвечает ошибкой {exc.code}: {exc.description}"
    return f"сбой опроса: {type(exc).__name__}"


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
        # Время последнего ЗАВЕРШЁННОГО оборота цикла — по нему сторож ловит
        # зависший цикл. Жив ли приём, отсюда не видно: оборот завершает и
        # неудачный заход.
        self._polled_at = time.monotonic()
        # Когда Telegram в последний раз ОТВЕТИЛ на getUpdates (пустой список —
        # тоже ответ). Пульс процесса считается по нему. Старт засчитан за ответ:
        # у свежего цикла есть DEAF_AFTER на первый заход, иначе каждая выкатка
        # начиналась бы с красного healthcheck.
        self._heard_at = time.monotonic()
        self._heard_marked_at: float | None = None
        # Причина глухоты, уже записанная в `poll_error`: одно и то же не пишется
        # в базу каждые пять секунд.
        self._reported: str | None = None
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

    def deaf_for(self) -> float:
        """Сколько секунд Telegram не отвечал на getUpdates."""
        return time.monotonic() - self._heard_at

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
            except TimeoutError as exc:
                log.warning("getUpdates @%s не ответил за %.0f с — обрываю заход",
                            self.username, limit)
                self._on_timeout()
                await self._on_failure(exc)
                await asyncio.sleep(ERROR_SLEEP)
                continue
            except tg.TelegramError as exc:
                log.warning("getUpdates @%s: %s", self.username, exc)
                if is_webhook_conflict(exc):
                    if await self._clear_webhook():
                        self._polled_at = time.monotonic()
                        continue  # вебхук снят — следующий заход сразу
                    if self._stop.is_set():
                        return    # вебхук чужой: бот приостановлен
                if exc.code == 0 and "timeout" in exc.description.lower():
                    self._on_timeout()
                else:
                    self._polled_at = time.monotonic()
                await self._on_failure(exc)
                await asyncio.sleep(ERROR_SLEEP)
                continue

            self._on_success()
            await self._mark_heard()
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
        self._polled_at = self._heard_at = time.monotonic()
        if self._timeouts and not self._short_poll:
            log.info("длинный опрос @%s снова работает", self.username)
        self._timeouts = 0
        if self._short_poll:
            # Короткий опрос работает — следующую попытку длинного отложим,
            # чтобы не дёргать сеть впустую каждый оборот.
            self._long_retry_at = max(self._long_retry_at,
                                      time.monotonic() + SHORT_POLL_SLEEP)

    # ------------------------------------------------ глухота: видно снаружи
    async def _on_failure(self, exc: BaseException) -> None:
        """Заход не удался. Человеку — только когда глухота стала фактом.

        Единичный сбой — норма сети, и писать о нём в базу значило бы пугать
        экран приложения каждой вспышкой. DEAF_AFTER без единого ответа — это бот,
        который не видит команд, и узнать об этом должен человек на экране, а не
        только тот, кто читает `docker logs`.
        """
        reason = describe_failure(exc)
        if self.deaf_for() < DEAF_AFTER or reason == self._reported:
            return
        if self._reported is None:
            log.error("бот @%s не получает апдейты %.0f с: %s",
                      self.username, self.deaf_for(), reason)
        if await self._write_state(_SQL_DEAF, reason[:500]):
            self._reported = reason

    async def _mark_heard(self) -> None:
        """Telegram ответил. В базу — не чаще HEARD_MARK_EVERY, после глухоты — сразу."""
        now = time.monotonic()
        if (self._reported is None and self._heard_marked_at is not None
                and now - self._heard_marked_at < HEARD_MARK_EVERY):
            return
        if not await self._write_state(_SQL_HEARD):
            return
        if self._reported is not None:
            log.info("бот @%s снова получает апдейты", self.username)
        self._heard_marked_at = now
        self._reported = None

    async def _write_state(self, sql: str, *args: object) -> bool:
        """Отметка о приёме в `tg_bots`. Недоступная база опрос не останавливает."""
        try:
            with tenant_scope(self.tenant_id):
                async with pool().acquire() as conn:
                    await conn.execute(sql, self.bot_ref, *args)
        except Exception as exc:
            log.warning("состояние приёма @%s не записано: %s", self.username, exc)
            return False
        return True

    # ------------------------------------------------------------ вебхук
    async def _clear_webhook(self) -> bool:
        """409 «webhook is active»: снять вебхук, если он наш, и слушать дальше.

        Пока у бота есть вебхук, Telegram не отдаёт опросом ни одного апдейта —
        только 409 на каждый заход. Поллер снимал вебхук лишь при старте, а вкладка
        «Бот» в приложении Битрикса ставила его при каждом сохранении токена:
        уже запущенный цикл глох до ближайшего перезапуска контейнера.

        Вебхук на наш же домен — след своей конфигурации: снимаем. На чужой адрес —
        токен у третьих лиц, и переписка чатов уходит туда: бот приостанавливается,
        как при проверке из приложения (`api/app_ui._recheck_bot`).
        True — вебхук снят, можно сразу повторить заход.
        """
        try:
            info = await tg.get_webhook_info(self._token)
        except tg.TelegramError as exc:
            log.warning("getWebhookInfo @%s: %s", self.username, exc)
            return False
        url = str(info.get("url") or "")
        if url and not is_own_webhook(url):
            await self._suspend("Вебхук бота указывает на посторонний адрес — похоже, "
                                "токен попал к третьим лицам. Перевыпустите его в "
                                "BotFather и введите новый на вкладке «Бот».")
            return False
        try:
            await tg.delete_webhook(self._token)
        except tg.TelegramError as exc:
            log.warning("вебхук @%s не снят: %s", self.username, exc)
            return False
        log.warning("с бота @%s снят вебхук: пока он стоял, Telegram не отдавал "
                    "апдейты опросом", self.username)
        return True

    async def _suspend(self, reason: str) -> None:
        log.error("ИНЦИДЕНТ: бот @%s приостановлен: %s", self.username, reason)
        self.stop()
        await self._write_state(_SQL_SUSPEND, reason[:500])

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
        # Шифротекст токена, с которым поднят цикл. Цикл держит токен в памяти, и
        # без этой сверки новый токен со вкладки «Бот» не доезжал до опроса вовсе:
        # цикл продолжал спрашивать старого бота. Шифротекст меняется при любом
        # сохранении, даже того же токена (случайный nonce), — и это к лучшему:
        # переподнятый цикл при старте снимает вебхук.
        self._tokens: dict[int, str] = {}

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

        alive = {r["id"]: r for r in rows}
        for bot_ref in list(self._tasks):
            task = self._tasks[bot_ref]
            poller = self._pollers[bot_ref]
            # Зависший цикл — не то же самое, что упавший: задача не `done()`,
            # исключения нет, снаружи всё выглядит работающим. Проверяем по
            # времени последнего оборота, иначе такой цикл живёт вечно и молчит.
            stalled = poller.idle_for() > STALL_AFTER
            row = alive.get(bot_ref)
            replaced = row is not None and row["token"] != self._tokens.get(bot_ref)
            if row is None or task.done() or stalled or replaced:
                if stalled:
                    log.error("поллер @%s завис: %.0f с без оборота — переподнимаю",
                              poller.username, poller.idle_for())
                elif replaced:
                    log.info("токен бота @%s сохранён заново — переподнимаю цикл",
                             poller.username)
                else:
                    log.info("поллер остановлен: bot_ref=%s", bot_ref)
                poller.stop()
                self._tasks.pop(bot_ref).cancel()
                self._pollers.pop(bot_ref, None)
                self._tokens.pop(bot_ref, None)

        for r in rows:
            if r["id"] in self._tasks:
                continue
            token = box.decrypt(
                r["token"], box.aad("tg_bots", "token", r["tenant_id"], r["bot_id"]))
            poller = BotPoller(r["id"], r["tenant_id"], r["bot_id"],
                               r["username"], token, int(r["update_offset"] or 0))
            self._pollers[r["id"]] = poller
            self._tokens[r["id"]] = r["token"]
            task = asyncio.create_task(poller.run())
            # Без этого исключение внутри задачи тонет: цикл просто исчезает, а в
            # логах остаётся только «поллер остановлен» без причины.
            task.add_done_callback(_report_death)
            self._tasks[r["id"]] = task

    def hearing(self) -> bool:
        """Идёт ли приём: каждый цикл оборачивается И получает ответы Telegram.

        Пустой реестр — это тоже «идёт»: ботов просто нет.

        Пульс раньше ставил супервизор за сам факт своего оборота, и 08.09.2026
        это стоило одиннадцати дней молчания: цикл опроса висел, супервизор
        исправно бился, контейнер числился `healthy`. После той правки пульс
        считался по обороту цикла — и 23.09.2026 контейнер снова был `healthy`
        при боте, который не отвечал ни на одну команду: оборотом засчитывался и
        неудачный заход. Здоровье процесса — это не «цикл крутится», а «Telegram
        отдаёт апдейты».
        """
        return all(p.idle_for() <= STALL_AFTER and p.deaf_for() <= DEAF_AFTER
                   for p in self._pollers.values())

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception("не удалось обновить список ботов")
            else:
                # Пульс — только когда Telegram отвечает. Зависший цикл `sync()`
                # переподнимет сам; глухой переподнимать бесполезно — причина
                # снаружи (прокси, вебхук, второй экземпляр) и записана в
                # `tg_bots.poll_error`. Healthcheck обязан покраснеть, а не
                # покрывать тишину.
                if self.hearing():
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
