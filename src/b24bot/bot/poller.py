"""Приём апдейтов через long polling.

Почему polling, а не вебхук. Проверено на боевом сервере 16.08.2026:
  * `api.telegram.org` с сервера недоступен напрямую — таймаут;
  * серверы Telegram, в свою очередь, НЕ МОГУТ достучаться до нашего домена:
    `getWebhookInfo` стабильно отдаёт `Connection timed out`, а в логах приложения
    ноль запросов на `/tg/`.

То есть фильтрация двусторонняя, и вебхук на этом хосте нежизнеспособен. Исходящие
вызовы идут прямым адресом Telegram, а SOCKS5 — запасной путь (`tg.Route`); приём —
long polling.
Обработчик вебхука в коде остаётся: он заработает без единой правки, если сервис
переедет на хост с прямым доступом.

Слышит ли бот Telegram на самом деле — отдельный вопрос, и отвечает на него
`bot/reception.py`: пульс процесса ставится по ответам Telegram и по его очереди,
а не по обороту цикла.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from b24bot.bot import commands, dispatch
from b24bot.bot.reception import Problem, Reception, is_webhook_conflict
from b24bot.core import heartbeat
from b24bot.core.config import get_settings
from b24bot.crypto import box
from b24bot.db.pool import pool, system_scope, tenant_scope
from b24bot.tg import api as tg

log = logging.getLogger(__name__)

# Часы и сон цикла. Монотонные часы — одни на весь приём (оборот, глухота, очередь);
# тесты подменяют оба и прогоняют часы работы за миллисекунды.
clock = time.monotonic
_sleep = asyncio.sleep

POLL_TIMEOUT = 25          # сколько Telegram держит соединение, секунд
IDLE_SLEEP = 1.0           # пауза после пустого ответа
ERROR_SLEEP = 5.0          # пауза после ошибки
REFRESH_BOTS_EVERY = 30.0  # как часто перечитываем список ботов

# Жёсткий предел на один заход в getUpdates. HTTP-клиенту таймаут уже задан
# (`tg.http_timeout_for`), но 08.09.2026 на боевом сервере он не сработал:
# запрос завис навсегда, поллер простоял 11 дней, Telegram копил апдейты, а
# контейнер всё это время числился `healthy`. Библиотечный таймаут — обещание
# библиотеки; этот — наше, и обойти его нечем.
#
# Предел — запас поверх худшего времени вызова клиента (`tg.deadline`), а не
# число: клиент перебирает пути к Telegram, и предел короче их суммы обрывал бы
# заход раньше, чем клиент узнает, что путь закрыт. Так и было 23.09.2026 на
# коротком опросе: предел 20 с совпадал с таймаутом клиента, и обрывал всегда он.
HARD_MARGIN = 5.0
# Сколько цикл может не завершать оборот, прежде чем считать его зависшим.
# Оборот — это getUpdates плюс обработка; при исправной сети он занимает секунды,
# при неисправной — упирается в предел захода и ERROR_SLEEP.
STALL_AFTER = 135.0

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

# Как часто спрашивать очередь Telegram и отчитываться о приёме в `tg_bots`. Экран
# считает на минуты; чаще — лишний вызов и лишняя запись на каждый короткий опрос.
CHECK_EVERY = 60.0
# Как часто повторять в логе сбой приёма, который всё ещё длится. Первая строка —
# сразу; дальше — чтобы `docker logs --since 1h` показал сбой, начавшийся утром.
# Стоящая очередь при отвечающем Telegram других строк в логе не оставляет вовсе.
RELOG_EVERY = 900.0

# Отчёт о приёме в `tg_bots` (миграция 0022), раз в CHECK_EVERY.
# heard_at — момент последнего ответа Telegram, пересчитанный из монотонных часов:
# до первого ответа не трогаем, «Telegram ответил при старте» было бы неправдой.
_SQL_RECEPTION = """
    UPDATE tg_bots
       SET heard_at = CASE WHEN $2::float8 IS NULL THEN heard_at
                           ELSE now() - make_interval(secs => $2::float8) END,
           poll_error = $3, queue_pending = $4, poll_checked_at = now()
     WHERE id = $1
"""
# `status` поллер трогает только в одном случае — чужой вебхук, то есть токен у
# третьих лиц.
_SQL_SUSPEND = ("UPDATE tg_bots SET status = 'suspended', last_error = $2, "
                "last_check_at = now() WHERE id = $1")


def is_own_webhook(url: str) -> bool:
    """Вебхук на наш же адрес приёма — след своей конфигурации, а не чужой."""
    return url.startswith(get_settings().public_base_url.rstrip("/") + "/tg/")


class BotPoller:
    """Один цикл на одного бота теннанта."""

    def __init__(self, bot_ref: int, tenant_id: int, bot_id: int, username: str,
                 token: str, offset: int, reception: Reception | None = None) -> None:
        self.bot_ref = bot_ref
        self.tenant_id = tenant_id
        self.bot_id = bot_id
        self.username = username
        self._token = token
        self._offset = offset
        self._stop = asyncio.Event()
        # Время последнего ЗАВЕРШЁННОГО оборота цикла — по нему сторож ловит
        # зависший цикл. Жив ли приём, отсюда не видно: оборот завершает и
        # неудачный заход. Это видно по `reception`.
        self._polled_at = clock()
        # Приём живёт в реестре и переживает перезапуск цикла (bot/reception.py).
        self.reception = reception if reception is not None else Reception(clock())
        # Опрос начинает с длинного и отступает на короткий, только увидев,
        # что длинный на этой сети не доживает до ответа.
        self._short_poll = False
        self._timeouts = 0
        self._long_retry_at = clock() + RETRY_LONG_EVERY

    def stop(self) -> None:
        self._stop.set()

    def idle_for(self) -> float:
        """Сколько секунд цикл не завершал оборот."""
        return clock() - self._polled_at

    def deaf_for(self) -> float:
        """Сколько секунд Telegram не отвечал на getUpdates."""
        return self.reception.deaf_for(clock())

    def healthy(self) -> bool:
        """Цикл не завис и приём в порядке — то, за что ставится пульс процесса."""
        return (self.idle_for() <= STALL_AFTER
                and self.reception.problem(clock()) is None)

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
            params: dict[str, Any] = {
                "offset": self._offset + 1,
                "timeout": self._poll_timeout(),
                "allowed_updates": tg.ALLOWED_UPDATES,
            }
            limit = tg.deadline("getUpdates", params) + HARD_MARGIN
            try:
                # Свой предел поверх клиентского: библиотечный таймаут однажды
                # уже не сработал, и цена этого — молчащий бот, неотличимый
                # снаружи от исправного.
                async with asyncio.timeout(limit):
                    updates = await tg.call(self._token, "getUpdates", params)
            except tg.TelegramInvalidToken as exc:
                log.error("токен бота @%s отвергнут (%s) — поллер остановлен",
                          self.username, exc.description)
                await self._mark_error(f"токен отвергнут: {exc.description}")
                return
            except TimeoutError as exc:
                log.warning("getUpdates @%s не ответил за %.0f с — обрываю заход",
                            self.username, limit)
                self._on_timeout()
                await self._after_failure(exc)
                continue
            except tg.TelegramError as exc:
                log.warning("getUpdates @%s: %s", self.username, exc)
                if is_webhook_conflict(exc):
                    if await self._clear_webhook():
                        self._polled_at = clock()
                        continue  # вебхук снят — следующий заход сразу
                    if self._stop.is_set():
                        return    # вебхук чужой: бот приостановлен
                if exc.code == 0 and "timeout" in exc.description.lower():
                    self._on_timeout()
                else:
                    self._polled_at = clock()
                await self._after_failure(exc)
                continue

            self._on_success()
            if updates:
                await self._handle(updates)
            await self._check_reception()
            if not updates:
                await _sleep(SHORT_POLL_SLEEP if self._short_poll else IDLE_SLEEP)

    async def _handle(self, updates: list[dict[str, Any]]) -> None:
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
        self._polled_at = clock()

    async def _after_failure(self, exc: BaseException) -> None:
        """Заход не удался: запомнить почему, отчитаться, если пора, и переждать."""
        self.reception.failed(exc, clock())
        await self._check_reception()
        await _sleep(ERROR_SLEEP)

    # ------------------------------------------------------- подстройка опроса
    def _poll_timeout(self) -> float:
        """Сколько просить Telegram держать соединение.

        Возврат к длинному опросу пробуется по времени, а не по числу удач:
        короткий опрос успешен всегда, и по удачам мы не вернулись бы никогда.
        """
        if self._short_poll and clock() >= self._long_retry_at:
            self._short_poll = False
            log.info("пробую вернуться к длинному опросу @%s", self.username)
        return 0.0 if self._short_poll else float(POLL_TIMEOUT)

    def _on_timeout(self) -> None:
        """Заход не дожил до ответа. Оборот засчитан: цикл жив, сеть — нет."""
        self._polled_at = clock()
        self._timeouts += 1
        if not self._short_poll and self._timeouts >= FALLBACK_AFTER:
            self._short_poll = True
            self._long_retry_at = clock() + RETRY_LONG_EVERY
            log.warning(
                "длинный опрос @%s не доживает до ответа (%d таймаута подряд) — "
                "перехожу на короткие запросы", self.username, self._timeouts)

    def _on_success(self) -> None:
        self._polled_at = clock()
        self.reception.answered(self._polled_at)
        if self._timeouts and not self._short_poll:
            log.info("длинный опрос @%s снова работает", self.username)
        self._timeouts = 0
        if self._short_poll:
            # Короткий опрос работает — следующую попытку длинного отложим,
            # чтобы не дёргать сеть впустую каждый оборот.
            self._long_retry_at = max(self._long_retry_at, clock() + SHORT_POLL_SLEEP)

    # ------------------------------------------------ приём: видно снаружи
    async def _check_reception(self) -> None:
        """Раз в CHECK_EVERY: спросить очередь Telegram и отчитаться о приёме.

        Отчёт — одна строка `tg_bots` на минуту: её читает экран приложения, и по
        возрасту `poll_checked_at` он видит, что служба бота вообще работает.
        Диагностика не имеет права уронить опрос: любой её сбой — строка в логе.
        """
        rec = self.reception
        if clock() < rec.next_check:
            return
        rec.next_check = clock() + CHECK_EVERY
        try:
            pending = await self._queue_depth()
            now = clock()
            rec.observe_queue(pending, self._offset, now)
            problem = rec.problem(now)
            self._log_problem(problem, now)
            await self._write_reception(problem, now)
        except Exception:
            log.exception("проверка приёма @%s не удалась", self.username)
        self._polled_at = clock()

    async def _queue_depth(self) -> int | None:
        """Сколько апдейтов Telegram держит для бота. None — не удалось узнать.

        `getWebhookInfo` — вызов только на чтение: getUpdates он не прерывает и
        ничего не подтверждает, а ответ у него в сотни байт, так что проходит и там,
        где крупный апдейт застревает. Предел свой, как у захода.
        """
        limit = tg.deadline("getWebhookInfo", None) + HARD_MARGIN
        try:
            async with asyncio.timeout(limit):
                info = await tg.get_webhook_info(self._token)
        except (TimeoutError, tg.TelegramError) as exc:
            log.warning("очередь @%s не узнать: %s", self.username,
                        str(exc) or type(exc).__name__)
            return None
        value = info.get("pending_update_count")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _log_problem(self, problem: Problem | None, now: float) -> None:
        """Сбой — в лог, когда начался и раз в RELOG_EVERY, пока длится; конец — тоже."""
        rec = self.reception
        kind = problem.kind if problem else None
        if kind == rec.reported and (kind is None or now - rec.reported_at < RELOG_EVERY):
            return
        if problem is None:
            log.info("приём @%s снова в порядке", self.username)
        else:
            log.error("приём @%s: %s (последний ответ Telegram %.0f с назад, "
                      "в очереди Telegram %s)", self.username, problem.text,
                      rec.deaf_for(now), "?" if rec.pending is None else rec.pending)
        rec.reported, rec.reported_at = kind, now

    async def _write_reception(self, problem: Problem | None, now: float) -> None:
        rec = self.reception
        heard_ago = None if rec.answered_at is None else max(0.0, now - rec.answered_at)
        await self._write_state(_SQL_RECEPTION, heard_ago,
                                problem.text[:500] if problem else None, rec.pending)

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
        # Состояние приёма на бота. Переподнятый сторожем цикл получает прежнее:
        # это тот же бот, и отсчёт глухоты и стоящей очереди не начинается заново.
        # Новый токен — новое: его приём ещё никто не проверял.
        self._receptions: dict[int, Reception] = {}

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
                if row is None or replaced:
                    self._receptions.pop(bot_ref, None)

        for r in rows:
            if r["id"] in self._tasks:
                continue
            token = box.decrypt(
                r["token"], box.aad("tg_bots", "token", r["tenant_id"], r["bot_id"]))
            reception = self._receptions.get(r["id"])
            if reception is None:
                reception = self._receptions[r["id"]] = Reception(clock())
            poller = BotPoller(r["id"], r["tenant_id"], r["bot_id"],
                               r["username"], token, int(r["update_offset"] or 0),
                               reception=reception)
            self._pollers[r["id"]] = poller
            self._tokens[r["id"]] = r["token"]
            task = asyncio.create_task(poller.run())
            # Без этого исключение внутри задачи тонет: цикл просто исчезает, а в
            # логах остаётся только «поллер остановлен» без причины.
            task.add_done_callback(_report_death)
            self._tasks[r["id"]] = task

    def hearing(self) -> bool:
        """Идёт ли приём: каждый цикл оборачивается И Telegram отдаёт ему апдейты.

        Пустой реестр — это тоже «идёт»: ботов просто нет.

        Дважды это правило было слабее, и оба раза контейнер был `healthy` при
        молчащем боте. 08.09.2026 пульс ставил супервизор за свой оборот — цикл
        опроса висел одиннадцать дней. 23.09.2026 — за оборот цикла, а оборотом
        засчитывался и оборванный заход: восемь часов ни одного апдейта. Здоровье
        процесса — это не «цикл крутится», а «Telegram отвечает, очередь пустеет,
        никто чужой её не забирает» (bot/reception.py).
        """
        return all(p.healthy() for p in self._pollers.values())

    async def run_forever(self) -> None:
        while True:
            try:
                await self.sync()
            except Exception:
                log.exception("не удалось обновить список ботов")
            else:
                # Пульс — только когда приём в порядке. Зависший цикл `sync()`
                # переподнимет сам; глухой переподнимать бесполезно — причина
                # снаружи и записана в `tg_bots.poll_error`. Healthcheck обязан
                # покраснеть, а не покрывать тишину.
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
