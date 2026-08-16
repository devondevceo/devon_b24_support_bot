"""Точка входа api-сервиса."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from b24bot.api.app_ui import router as app_router
from b24bot.api.b24 import router as b24_router
from b24bot.api.miniapp import ApiError, error_response, portal_failure
from b24bot.api.miniapp import router as miniapp_router
from b24bot.api.tg_webhook import router as tg_router
from b24bot.b24.errors import B24Error
from b24bot.b24.tokens import NeedsReauth
from b24bot.core.config import get_settings
from b24bot.core.logging import setup as log_setup
from b24bot.db.pool import close_pool, init_pool, pool

log = logging.getLogger(__name__)

# Мини-апп на Telegram Desktop и в вебе открывается в iframe клиента Telegram,
# поэтому frame-ancestors перечисляет его домены поимённо. Общий '*' здесь
# означал бы, что страницу может встроить кто угодно и снимать по ней клики.
TELEGRAM_FRAME_ANCESTORS = ("https://web.telegram.org https://webk.telegram.org "
                            "https://webz.telegram.org")
MINIAPP_CSP = (
    "default-src 'self'; "
    # telegram.org — источник официального telegram-web-app.js. Без него страница
    # не получает ни initData, ни темы клиента, то есть мини-аппом не является.
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    f"frame-ancestors {TELEGRAM_FRAME_ANCESTORS}"
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    log_setup(s.log_level)
    await init_pool()
    logging.getLogger(__name__).info("api запущен, домен %s", s.domain)
    yield
    await close_pool()


app = FastAPI(title="devon b24 support bot", lifespan=lifespan, docs_url=None,
              redoc_url=None)
app.include_router(b24_router)
app.include_router(app_router)
app.include_router(tg_router)
app.include_router(miniapp_router)


# --------------------------------------------------------------------- ошибки
@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return error_response(exc)


@app.exception_handler(NeedsReauth)
@app.exception_handler(B24Error)
async def _portal_error(request: Request, exc: Exception) -> Response:
    """Отказ портала на любом пути мини-аппа — один и тот же контракт ответа.

    Часть вызовов идёт не из обработчика, а из общего кода (`domain/tasks`,
    `bot/comments`), и ловить их в каждом месте — способ однажды забыть.
    """
    log.info("портал отказал на %s: %s", request.url.path, exc)
    return error_response(portal_failure(exc))


# ------------------------------------------------------------------- мини-апп
def _miniapp_dir() -> Path | None:
    path = Path(get_settings().miniapp_dist)
    return path if (path / "index.html").is_file() else None


_DIST = _miniapp_dir()

if _DIST is not None:
    app.mount("/miniapp/assets", StaticFiles(directory=_DIST / "assets"),
              name="miniapp-assets")

    @app.get("/miniapp")
    @app.get("/miniapp/{path:path}")
    async def miniapp_page(path: str = "") -> Response:
        """Одностраничное приложение: любой путь отдаёт index.html.

        Заголовки ставим здесь, а не в Traefik: security-headers@file содержит
        frameDeny, а мини-апп обязан открываться в iframe клиентов Telegram.
        """
        resp = FileResponse(_DIST / "index.html")  # type: ignore[operator]
        resp.headers["Content-Security-Policy"] = MINIAPP_CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Индексация мини-аппа поисковиками не нужна и вредна: страница
        # бесполезна без Telegram, а домен светить незачем.
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        return resp
else:  # pragma: no cover - на машине разработчика фронтенд может быть не собран
    log.warning("фронтенд мини-аппа не найден в %s — /miniapp отдавать нечего",
                get_settings().miniapp_dist)


@app.get("/health")
async def health() -> JSONResponse:
    checks: dict[str, object] = {}
    try:
        async with pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"fail: {type(exc).__name__}"
    checks["miniapp"] = "ok" if _DIST is not None else "нет сборки"
    ok = all(v == "ok" for k, v in checks.items() if k != "miniapp")
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"service": "devon b24 support bot", "state": "ok"})
