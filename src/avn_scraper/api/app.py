from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import Config, load_config
from ..storage.postgres import database_url
from .routers import accepted, news, scrape
from .scheduler import Scheduler
from .scrape import ScrapeManager

log = logging.getLogger(__name__)

# The service answers only to its own name. A page elsewhere can point a domain of its
# own at this machine (DNS rebinding); its requests then carry that domain as Host.
LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1", "[::1]", "testserver"]


def _last_full_run(pool: ConnectionPool) -> datetime | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT max(started_at) AS at FROM runs WHERE finished_at IS NOT NULL "
                           "AND stats->'only_sources' IS NULL").fetchone()
    return row["at"]


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def create_app(cfg: Config | None = None, scrapes: ScrapeManager | None = None, schedule: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    url = database_url()
    pool = ConnectionPool(url, min_size=1, max_size=4, open=False, timeout=5,
                          kwargs={"autocommit": True, "row_factory": dict_row})
    scrapes = scrapes or ScrapeManager(cfg)
    hour, minute = str(cfg.settings.get("daily_at", "07:00")).split(":")
    scheduler = Scheduler(scrapes, lambda: _last_full_run(pool), time(int(hour), int(minute)), ZoneInfo(cfg.timezone))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool.open(wait=False)  # the database may still be starting after a boot
        task = asyncio.create_task(scheduler.loop()) if schedule else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        pool.close()

    app = FastAPI(title="AVN news service", version="0.1.0", lifespan=lifespan)
    app.state.pool = pool
    app.state.scrapes = scrapes
    app.state.scheduler = scheduler
    app.state.reviewing = os.environ.get("AVN_REVIEW") == "on"
    app.state.category_order = list((cfg.keywords.get("categories") or {}).keys())
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def bad_input(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        return _error(400, f"Bad {first['loc'][-1]}: {first['msg']}")

    @app.exception_handler(PoolTimeout)
    @app.exception_handler(psycopg.OperationalError)
    async def database_down(_: Request, exc: Exception) -> JSONResponse:
        log.error("database unavailable: %s", exc)
        return _error(503, "The news database could not be reached.")

    for module in (news, accepted, scrape):
        app.include_router(module.router)
    return app
