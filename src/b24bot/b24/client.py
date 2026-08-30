"""HTTP-клиент Битрикс24: лимитер, batch, keyset-пагинация, разбор ошибок.

Инвариант И-4: адрес портала собирается САМИ из проверенного домена. Значения
SERVER_ENDPOINT / client_endpoint из входящих запросов не используются никогда.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpx

from b24bot.b24 import errors
from b24bot.b24.limiter import Lane, PortalLimiter
from b24bot.b24.mapping import encode_params
from b24bot.core.config import is_trusted_portal_domain

log = logging.getLogger(__name__)

BATCH_MAX = 50
PAGE_SIZE = 50
MAX_ATTEMPTS = 4
REQUEST_TIMEOUT = 65.0  # потолок одного запроса на стороне Битрикса — 60 с

JsonDict = dict[str, Any]
TokenProvider = Callable[[], Awaitable[str]]
# Журнал вызова: (метод, успех, код ошибки, длительность в мс). Клиент не знает
# ни теннанта, ни базы — кто знает, тот и передаёт наблюдателя (domain/access.py).
CallObserver = Callable[[str, bool, str | None, int], Awaitable[None]]


class B24Client:
    """Один клиент — один портал.

    token_provider вызывается перед каждым запросом и обязан сам решать вопрос
    протухшего access_token (см. tokens.TokenStore). Клиент про refresh не знает.
    """

    def __init__(self, domain: str, token_provider: TokenProvider,
                 limiter: PortalLimiter, *, http: httpx.AsyncClient | None = None,
                 observer: CallObserver | None = None) -> None:
        if not is_trusted_portal_domain(domain):
            raise ValueError(f"недоверенный домен портала: {domain!r}")
        self.domain = domain
        self._token = token_provider
        self._limiter = limiter
        self._http = http
        self._own_http = http is None
        self._observer = observer

    async def __aenter__(self) -> B24Client:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}/rest/"

    # ------------------------------------------------------------------ вызовы
    async def call(self, method: str, params: JsonDict | None = None, *,
                   lane: Lane = Lane.INTERACTIVE) -> Any:
        """Один REST-вызов с ретраями. Возвращает содержимое result."""
        data = await self.call_envelope(method, params, lane=lane)
        return data.get("result") if isinstance(data, dict) else data

    async def call_total(self, method: str, params: JsonDict | None = None, *,
                         lane: Lane = Lane.INTERACTIVE) -> int:
        """Сколько записей у портала ВСЕГО по этому запросу.

        Нужно там, где метод отдаёт меньше, чем есть: у `task.elapseditem.getlist`
        постраничность сломана (docs/00-portal-facts.md §5.2), и `total` из
        конверта — единственный способ отличить «показали всё» от «показали
        первые 50 из трёхсот».
        """
        data = await self.call_envelope(method, params, lane=lane)
        return int(data.get("total") or 0) if isinstance(data, dict) else 0

    async def call_envelope(self, method: str, params: JsonDict | None = None, *,
                            lane: Lane = Lane.INTERACTIVE) -> Any:
        """Тот же вызов, но ответ целиком: снаружи `result` лежат `total` и `next`.

        Обёртка вокруг `_call_envelope` существует ради наблюдателя: журнал
        вызовов (`b24_call_log`) — требование Маркета к серверным приложениям,
        и писать его обязан ровно один слой, а не каждый вызывающий. Наблюдатель
        не имеет права уронить вызов: журнал — свидетель, а не участник.
        """
        started = time.monotonic()
        error_code: str | None = None
        try:
            return await self._call_envelope(method, params, lane=lane)
        except errors.B24Error as exc:
            error_code = exc.code
            raise
        except Exception:
            error_code = "EXCEPTION"
            raise
        finally:
            if self._observer is not None:
                duration_ms = int((time.monotonic() - started) * 1000)
                try:
                    await self._observer(method, error_code is None, error_code,
                                         duration_ms)
                except Exception:
                    log.exception("наблюдатель вызова %s упал", method)

    async def _call_envelope(self, method: str, params: JsonDict | None = None, *,
                             lane: Lane = Lane.INTERACTIVE) -> Any:
        body = encode_params(params or {})
        last: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._limiter.acquire(lane)
            body["auth"] = await self._token()
            try:
                assert self._http is not None, "клиент используется вне async with"
                resp = await self._http.post(self.base_url + method + ".json", data=body)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = errors.B24Transport("TRANSPORT", str(exc)[:200], method)
                await self._sleep_backoff(attempt)
                continue

            self._limiter.observe(data.get("time") if isinstance(data, dict) else None)

            if isinstance(data, dict) and data.get("error"):
                failure = errors.classify(str(data.get("error")),
                                          str(data.get("error_description") or ""),
                                          method, resp.status_code)
                if isinstance(failure, errors.B24QueryLimit | errors.B24OperatingLimit):
                    # Выбранный operating не восстановится за секунды: ждать дольше.
                    cooldown = 30.0 if isinstance(failure, errors.B24OperatingLimit) else 2.0
                    self._limiter.penalize(cooldown)
                    last = failure
                    await self._sleep_backoff(attempt)
                    continue
                raise failure  # auth, права, не найдено, прочее — ретрай бессмыслен

            return data

        raise last or errors.B24Error("UNKNOWN", "исчерпаны попытки", method)

    async def _sleep_backoff(self, attempt: int) -> None:
        # random здесь — джиттер, чтобы параллельные воркеры не били в портал в такт.
        # Криптостойкость не нужна и не подразумевается.
        jitter = 0.7 + random.random() * 0.6  # noqa: S311
        delay = min(30.0, (2 ** attempt) * 0.5) * jitter
        log.info("повтор через %.1f c (попытка %d)", delay, attempt)
        await asyncio.sleep(delay)

    # ------------------------------------------------------------------- batch
    async def batch(self, commands: dict[str, tuple[str, JsonDict]], *,
                    halt_on_error: bool = False,
                    lane: Lane = Lane.INTERACTIVE) -> dict[str, Any]:
        """До 50 команд одним HTTP-вызовом.

        Экономит частотный лимит и НЕ экономит operating: время выполнения самих
        методов от упаковки не уменьшается.
        """
        if len(commands) > BATCH_MAX:
            raise ValueError(f"в batch максимум {BATCH_MAX} команд, передано {len(commands)}")

        cmd: dict[str, str] = {}
        for key, (method, params) in commands.items():
            query = "&".join(f"{k}={v}" for k, v in encode_params(params or {}).items())
            cmd[key] = f"{method}?{query}" if query else method

        result = await self.call("batch", {"halt": int(halt_on_error), "cmd": cmd}, lane=lane)
        result = result or {}

        for key, err in (result.get("result_error") or {}).items():
            code = err.get("error") if isinstance(err, dict) else str(err)
            desc = err.get("error_description", "") if isinstance(err, dict) else ""
            log.warning("batch[%s] -> %s %s", key, code, desc)

        return {
            "result": result.get("result") or {},
            "errors": result.get("result_error") or {},
            "total": result.get("result_total") or {},
            "next": result.get("result_next") or {},
        }

    # -------------------------------------------------------------- пагинация
    async def list_all(self, method: str, params: JsonDict | None = None, *,
                       items_key: str | None = None, id_field: str = "ID",
                       max_items: int = 10_000,
                       lane: Lane = Lane.BACKGROUND) -> list[JsonDict]:
        """Выкачать всё через keyset: order[ID]=asc + filter[>ID] + start=-1.

        start=-1 отключает подсчёт COUNT, который и есть главный тормоз на больших
        объёмах. Обычный постраничный обход по start здесь не используется намеренно.
        """
        params = dict(params or {})
        params.setdefault("order", {id_field: "asc"})
        params["start"] = -1

        out: list[JsonDict] = []
        last_id: int | None = None

        while len(out) < max_items:
            page_params = dict(params)
            flt = dict(page_params.get("filter") or {})
            if last_id is not None:
                flt[f">{id_field}"] = last_id
            page_params["filter"] = flt

            raw = await self.call(method, page_params, lane=lane)
            items = self._extract_items(raw, items_key)
            if not items:
                break

            out.extend(items)
            camel = id_field.lower() if id_field == "ID" else id_field
            new_last = items[-1].get(camel) or items[-1].get(id_field)
            if new_last is None:
                break
            last_id = int(new_last)

            if len(items) < PAGE_SIZE:
                break

        return out[:max_items]

    @staticmethod
    def _extract_items(raw: Any, items_key: str | None) -> list[JsonDict]:
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(raw, dict):
            if items_key and isinstance(raw.get(items_key), list):
                return list(raw[items_key])
            for key in ("tasks", "items", "result"):
                if isinstance(raw.get(key), list):
                    return list(raw[key])
        return []

    # ------------------------------------------------------------- утилита
    async def call_many(self, calls: Iterable[tuple[str, str, JsonDict]], *,
                        lane: Lane = Lane.BACKGROUND) -> dict[str, Any]:
        """Разбить произвольное число вызовов на batch-пачки по 50."""
        merged: dict[str, Any] = {}
        chunk: dict[str, tuple[str, JsonDict]] = {}
        for key, method, params in calls:
            chunk[key] = (method, params)
            if len(chunk) == BATCH_MAX:
                merged.update((await self.batch(chunk, lane=lane))["result"])
                chunk = {}
        if chunk:
            merged.update((await self.batch(chunk, lane=lane))["result"])
        return merged
