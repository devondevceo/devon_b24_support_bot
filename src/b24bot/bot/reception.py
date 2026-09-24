"""Слышит ли бот Telegram: приём апдейтов глазами healthcheck-а и человека.

Дважды бот замолкал при зелёном всём, и оба раза зелёным отвечал не тот вопрос.

* **08.09.2026, одиннадцать дней.** Пульс ставил супервизор за свой оборот, а цикл
  опроса висел.
* **23.09.2026, восемь часов** (13:38–21:30 UTC по `tg_bots.last_check_at`). Пульс
  ставился за оборот цикла, а оборотом засчитывался и оборванный заход: прокси резал
  крупный апдейт в голове очереди, каждый `getUpdates` кончался таймаутом, и цикл
  честно крутился вхолостую. Контейнер `healthy`, `/health` — `ok`, экран Битрикса —
  «Интеграция работает», в очереди Telegram — 19 непрочитанных апдейтов.

Отсюда три сигнала, каждый со своей стороны:

1. **Ответ Telegram на `getUpdates`.** Пустой список — тоже ответ. Ни одного ответа
   дольше `DEAF_AFTER` — бот глух, что бы ни происходило с циклом.
2. **Очередь глазами Telegram** (`getWebhookInfo.pending_update_count`) —
   единственное число, которое отличает «в чатах тихо» от «мы не забираем
   сообщения». Очередь не пуста, а offset не двигается дольше `QUEUE_STUCK_AFTER` —
   бот не забирает сообщения, даже если каждый `getUpdates` отвечает.
3. **409 «terminated by other getUpdates request».** Telegram прямо говорит, что тем
   же токеном опрашивает кто-то ещё. Повторяется — это вторая копия бота, и часть
   сообщений уходит ей, а не в чаты.

Любой из них гасит пульс (`poller.PollerRegistry.hearing`), а раз в минуту
(`poller.CHECK_EVERY`) состояние пишется в `tg_bots` — его читает экран приложения
(`on_screen`).

Чего здесь нет намеренно:

* **Автоматического сброса очереди** (`deleteWebhook(drop_pending_updates=True)`,
  которым вернули приём 08.09). Он стирает сообщения людей; решает человек.
* **Перезапуска глухого цикла.** Причина глухоты снаружи — сеть, прокси, вебхук,
  вторая копия, — и новый цикл упрётся в неё же. Зато перезапуск стёр бы выученное
  отступление на короткий опрос и начал бы отсчёт глухоты заново: пульс вспыхивал бы
  зелёным на каждом перезапуске. Зависший цикл — другое дело, его переподнимает
  сторож (`poller.STALL_AFTER`), и это состояние переживает перезапуск.
"""
from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from b24bot.core.logging import scrub
from b24bot.tg import api as tg

# Столько без единого ответа на getUpdates — глухота. Длиннее отступления на
# короткий опрос (poller.FALLBACK_AFTER таймаутов по пределу захода плюс паузы —
# около 200 с при двух путях к Telegram): сеть, где длинный опрос не живёт, а
# короткий живёт, глухотой не считается — бот в ней работает.
DEAF_AFTER = 300.0
# Столько очередь Telegram может не пустеть при стоящем offset. Исправный бот
# забирает апдейт за секунды: длинный опрос отдаёт его сразу, как тот появился.
QUEUE_STUCK_AFTER = 300.0
# Вторая копия: столько 409 «другой getUpdates» за окно. Один бывает от ручной
# пробы с тем же токеном — не повод краснеть; три за пять минут — чужой цикл,
# который перебивает наш и забирает сообщения себе.
CONFLICTS_TO_FLAG = 3
CONFLICT_WINDOW = 300.0
# Экран: столько служба бота может не отчитываться, прежде чем считать её
# неработающей. Отчёт раз в минуту (`poller.CHECK_EVERY`); запас — на заход,
# который при сбое сети тянется до своего предела, и на перезапуск контейнера.
STATE_STALE_AFTER = 600.0

# Логин и пароль прокси в тексте ошибки транспорта. Причина глухоты уходит на экран
# администратора теннанта, а исключение httpx вправе процитировать адрес целиком.
_CREDENTIALS = re.compile(r"(\w+://)[^/\s@]+@")

CONFLICT_TEXT = ("Этого бота опрашивает ещё одна программа с тем же токеном и забирает "
                 "сообщения себе. Если это не ваш тестовый запуск, токен попал к "
                 "посторонним: отзовите его в @BotFather и введите новый на вкладке «Бот»")


def is_webhook_conflict(exc: tg.TelegramError) -> bool:
    """409, который означает вебхук на боте, а не второй экземпляр опроса.

    Telegram отвечает так в двух случаях: на `getUpdates`, пока вебхук стоит, и
    обрывая уже висящий long poll в момент `setWebhook`. Лечится одинаково.
    """
    return exc.code == 409 and "webhook" in exc.description.lower()


def is_other_poller(exc: BaseException) -> bool:
    """409 «terminated by other getUpdates request»: тем же токеном опрашивает кто-то ещё."""
    return (isinstance(exc, tg.TelegramError) and exc.code == 409
            and not is_webhook_conflict(exc))


def is_timeout(exc: BaseException) -> bool:
    """Заход не дождался ответа: наш предел или таймаут чтения в клиенте."""
    return isinstance(exc, TimeoutError) or (
        isinstance(exc, tg.TelegramError) and exc.code == 0
        and "timeout" in exc.description.lower())


def describe_failure(exc: BaseException) -> str:
    """Почему заход в getUpdates не удался — человеческим языком.

    Текст уходит в `tg_bots.poll_error` и на экран администратора теннанта, поэтому
    проходит через скраббер логов: токена в описаниях Telegram нет, но экран — это
    ответ API, и И-7 здесь не проверяется на честное слово.
    """
    if is_timeout(exc):
        return ("Сервер не дожидается ответа Telegram на запрос новых сообщений "
                "(таймаут связи)")
    if isinstance(exc, tg.TelegramError):
        if is_webhook_conflict(exc):
            return "На боте установлен вебхук, и Telegram не отдаёт сообщения опросом"
        if exc.code == 409:
            return "Этого бота опрашивает ещё одна программа с тем же токеном"
        if exc.code == 0:
            detail = _CREDENTIALS.sub(r"\1", exc.description.removeprefix("транспорт: "))
            return f"Сервер не может соединиться с Telegram ({scrub(detail)})"
        return (f"Telegram отвечает на запрос новых сообщений ошибкой {exc.code}: "
                f"{scrub(exc.description)}")
    return f"Сбой опроса Telegram: {type(exc).__name__}"


def span(seconds: float) -> str:
    """«7 мин», «2 ч», «3 дн.» — сколько длится сбой или прошло с отметки."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч"
    return f"{hours // 24} дн."


def ago(seconds: float) -> str:
    """«3 мин назад» — для отметок, которые человек сверяет с тем, что видел в чате."""
    return f"{span(seconds)} назад"


class Problem(NamedTuple):
    """Что не так с приёмом. `kind` — для лога (о чём уже сказано), `text` — для экрана."""
    kind: str   # deaf | conflict | stuck
    text: str


@dataclass
class Reception:
    """Состояние приёма одного бота.

    Живёт в реестре поллеров, а не в цикле, и переживает перезапуск цикла: цикл,
    переподнятый сторожем, — тот же бот, и отсчёт глухоты не начинается заново.
    Иначе каждый перезапуск давал бы пульсу новую отсрочку, и цикл, зависающий раз
    в две минуты, числился бы здоровым вечно. Время — монотонные секунды
    (`poller.clock`), их передаёт вызывающий.
    """
    # Когда начали слушать. До первого ответа глухота считается от этой точки:
    # свежему процессу положено DEAF_AFTER на первый заход, иначе каждая выкатка
    # начиналась бы с красного healthcheck.
    started_at: float
    answered_at: float | None = None     # последний ответ Telegram на getUpdates
    failure: str | None = None           # почему не удался последний заход
    pending: int | None = None           # pending_update_count; None — не знаем
    stuck_since: float | None = None     # очередь не пуста, а offset стоит — с тех пор
    stuck_offset: int = 0
    conflicts: deque[float] = field(default_factory=deque)
    next_check: float = 0.0              # когда снова спросить очередь и отчитаться
    reported: str | None = None          # вид сбоя, о котором уже сказано в логе
    reported_at: float = 0.0

    def answered(self, now: float) -> None:
        self.answered_at = now
        self.failure = None

    def failed(self, exc: BaseException, now: float) -> None:
        self.failure = describe_failure(exc)
        if is_other_poller(exc):
            self.conflicts.append(now)

    def deaf_for(self, now: float) -> float:
        """Сколько секунд Telegram не отвечал на getUpdates."""
        since = self.started_at if self.answered_at is None else self.answered_at
        return now - since

    def observe_queue(self, pending: int | None, offset: int, now: float) -> None:
        """Очередь со стороны Telegram. Стоит — только если не пуста И offset не двигался.

        Непустая очередь сама по себе — норма: проверка может прийти между разбором
        пачки и её подтверждением следующим getUpdates. Сбой — когда она не пустеет,
        а мы не продвигаемся ни на один апдейт. Неизвестная длина (Telegram не ответил
        на проверку) окно не сбрасывает: offset за это время не сдвинулся.
        """
        self.pending = pending
        if pending is None:
            return
        if pending == 0:
            self.stuck_since = None
        elif self.stuck_since is None or offset != self.stuck_offset:
            self.stuck_since = now
        self.stuck_offset = offset

    def stuck_for(self, now: float) -> float:
        return 0.0 if self.stuck_since is None else now - self.stuck_since

    def recent_conflicts(self, now: float) -> int:
        while self.conflicts and now - self.conflicts[0] > CONFLICT_WINDOW:
            self.conflicts.popleft()
        return len(self.conflicts)

    def problem(self, now: float) -> Problem | None:
        """Что не так с приёмом — или None. Порядок — от самого полного отказа."""
        if self.deaf_for(now) > DEAF_AFTER:
            return Problem("deaf", self.failure
                           or "Telegram не отвечает на запрос новых сообщений")
        if self.recent_conflicts(now) >= CONFLICTS_TO_FLAG:
            return Problem("conflict", CONFLICT_TEXT)
        stuck = self.stuck_for(now)
        if stuck > QUEUE_STUCK_AFTER:
            return Problem("stuck", f"В очереди Telegram ждут разбора: {self.pending}, "
                                    f"а бот не забрал ни одного сообщения за {span(stuck)}")
        return None


def on_screen(bot: Mapping[str, Any] | None) -> str | None:
    """Что сказать о приёме на экране приложения — или None, если сказать нечего.

    Читает то, что пишет поллер: причину (`poll_error`) и отметки `heard_at` и
    `poll_checked_at` в виде возраста в секундах (`heard_ago`, `poll_checked_ago`).
    Причину пишет живая служба бота; если она не работает, писать некому, и тогда
    говорит возраст её последнего отчёта. NULL в нём — «ещё не отчитывалась», а не
    сбой: так выглядит минута после выкатки с новой колонкой.
    """
    if (bot is None or bot.get("mode") != "polling"
            or bot.get("status") not in ("active", "pending")):
        return None
    heard = bot.get("heard_ago")
    last = f" Последний ответ Telegram — {ago(heard)}." if heard is not None else ""
    checked = bot.get("poll_checked_ago")
    if checked is not None and checked > STATE_STALE_AFTER:
        return (f"Служба бота последний раз отчиталась {ago(checked)}: сообщения из "
                f"чатов не забираются, команды остаются без ответа.{last}")
    reason = bot.get("poll_error")
    if reason:
        return f"{reason}.{last}"
    return None
