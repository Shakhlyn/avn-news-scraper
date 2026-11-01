"""The AVN news service. `avn serve` runs it; so does
`uvicorn avn_scraper.main:create_app --factory`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import time
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api.v1.router import api_router
from .core.config import Settings
from .core.database import last_full_run, make_pool
from .core.errors import add_error_handlers
from .core.limiter import limiter
from .scraper.config import Config, load_config
from .services.scheduler import Scheduler
from .services.scrape_manager import ScrapeManager


def create_app(cfg: Config | None = None, scrapes: ScrapeManager | None = None, schedule: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    settings = Settings()
    pool = make_pool(settings.database_url, settings.db_pool_max)
    scrapes = scrapes or ScrapeManager(cfg)
    hour, minute = str(cfg.settings.get("daily_at", "07:00")).split(":")
    scheduler = Scheduler(scrapes, lambda: last_full_run(pool), time(int(hour), int(minute)), ZoneInfo(cfg.timezone))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await pool.open(wait=False)  # the database may still be starting after a boot
        task = asyncio.create_task(scheduler.loop()) if schedule else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await scrapes.stop()
        await pool.close()

    app = FastAPI(title="AVN news service", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = pool
    app.state.scrapes = scrapes
    app.state.scheduler = scheduler
    app.state.limiter = limiter
    app.state.category_order = list((cfg.keywords.get("categories") or {}).keys())

    # The last one added runs first.
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["cache-control"] = "no-store"
        response.headers["x-content-type-options"] = "nosniff"
        return response

    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["content-type"])

    add_error_handlers(app)
    app.include_router(api_router)
    return app
