"""Точка входа api-сервиса."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from b24bot.api.app_ui import router as app_router
from b24bot.api.b24 import router as b24_router
from b24bot.api.tg_webhook import router as tg_router
from b24bot.core.config import get_settings
from b24bot.core.logging import setup as log_setup
from b24bot.db.pool import close_pool, init_pool, pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    log_setup(s.log_level)
    await init_pool()
    logging.getLogger(__name__).info("api запущен, домен %s", s.domain)
    yield
    await close_pool()


app = FastAPI(title="devon b24 support bot", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(b24_router)
app.include_router(app_router)
app.include_router(tg_router)


@app.get("/health")
async def health() -> JSONResponse:
    checks: dict[str, object] = {}
    try:
        async with pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"fail: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse({"service": "devon b24 support bot", "state": "skeleton (SPIKE A)"})
