"""Лимитер портала. Две независимые оси, обе реальны (docs/00-portal-facts.md §4).

Ось 1 — частота: leaky bucket, 2 запроса/с, ёмкость 50 (тарифы кроме Enterprise).
Ось 2 — ресурсоёмкость: operating, 420 секунд в скользящем окне 600 секунд.

batch помогает против первой оси и НЕ помогает против второй: он экономит HTTP-вызовы,
но operating считает время выполнения самих методов.

Две полосы движения. Интерактивная — то, что ждёт живой человек в чате. Фоновая —
синхронизация, отчёты, дозапрос чужих задач; она уступает всегда и первой замирает
при приближении к бюджету operating.
"""
from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field
from typing import Any


class Lane(enum.StrEnum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"


DEFAULT_RATE = 2.0
DEFAULT_CAPACITY = 50.0
OPERATING_LIMIT = 420.0
OPERATING_WINDOW = 600.0
# Доля бюджета operating, ниже которой фоновая полоса останавливается,
# чтобы интерактивным запросам всегда оставался запас.
BACKGROUND_RESERVE = 0.30


@dataclass
class PortalLimiter:
    """Состояние на один портал. Внутри одного процесса.

    Межпроцессная координация появится вместе с сервисом worker: состояние переезжает
    в таблицу b24_portal_budget. До тех пор процесс api — единственный, кто ходит в портал.
    """

    rate: float = DEFAULT_RATE
    capacity: float = DEFAULT_CAPACITY
    operating_limit: float = OPERATING_LIMIT

    _tokens: float = field(default=0.0, init=False)
    _updated_at: float = field(default_factory=time.monotonic, init=False)
    _operating_spent: float = field(default=0.0, init=False)
    _operating_reset_at: float | None = field(default=None, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        # Ведро стартует полным именно СВОЕЙ ёмкости. Константа по умолчанию здесь
        # означала бы, что лимитер с capacity=5 начинает жизнь с 50 токенами.
        self._tokens = self.capacity

    # ------------------------------------------------------------------ ведро
    def _refill(self, now: float) -> None:
        elapsed = now - self._updated_at
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated_at = now

    def _operating_ratio(self, now_wall: float) -> float:
        if self._operating_reset_at and now_wall >= self._operating_reset_at:
            self._operating_spent = 0.0
            self._operating_reset_at = None
        return self._operating_spent / self.operating_limit if self.operating_limit else 0.0

    # ---------------------------------------------------------------- публично
    async def acquire(self, lane: Lane = Lane.INTERACTIVE) -> None:
        """Дождаться права сделать один запрос."""
        while True:
            async with self._lock:
                now, wall = time.monotonic(), time.time()
                ratio = self._operating_ratio(wall)

                if lane is Lane.BACKGROUND and ratio >= (1 - BACKGROUND_RESERVE):
                    wait = max(1.0, (self._operating_reset_at or wall + 30) - wall)
                else:
                    self._refill(now)
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return
                    wait = (1.0 - self._tokens) / self.rate
            await asyncio.sleep(min(wait, 60.0))

    def observe(self, time_block: dict[str, Any] | None) -> None:
        """Обновить бюджет operating из блока time каждого ответа портала."""
        if not time_block:
            return
        op = time_block.get("operating")
        if isinstance(op, (int, float)):
            self._operating_spent = float(op)
        reset = time_block.get("operating_reset_at")
        if isinstance(reset, (int, float)):
            self._operating_reset_at = float(reset)

    def penalize(self, seconds: float) -> None:
        """Портал ответил 503/429 — опустошаем ведро, чтобы не долбить его дальше."""
        self._tokens = 0.0
        self._updated_at = time.monotonic() + max(0.0, seconds - 1.0 / self.rate)

    @property
    def stats(self) -> dict[str, float | None]:
        return {
            "tokens": round(self._tokens, 2),
            "operating_spent": round(self._operating_spent, 2),
            "operating_limit": self.operating_limit,
            "operating_reset_at": self._operating_reset_at,
        }


class LimiterRegistry:
    """По лимитеру на портал: бюджет общий у всех наших вызовов к одному теннанту."""

    def __init__(self) -> None:
        self._by_tenant: dict[int, PortalLimiter] = {}

    def for_tenant(self, tenant_id: int) -> PortalLimiter:
        if tenant_id not in self._by_tenant:
            self._by_tenant[tenant_id] = PortalLimiter()
        return self._by_tenant[tenant_id]
