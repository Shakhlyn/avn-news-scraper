"""Every error is {"error": "..."}, which is what the site's pages read."""

from __future__ import annotations

import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException

log = logging.getLogger(__name__)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def bad_input(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        return _error(400, f"Bad {first['loc'][-1]}: {first['msg']}")

    @app.exception_handler(RateLimitExceeded)
    async def too_many(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        return _error(429, f"Too many requests: {exc.detail}.")

    @app.exception_handler(PoolTimeout)
    @app.exception_handler(psycopg.OperationalError)
    async def database_down(_: Request, exc: Exception) -> JSONResponse:
        log.error("database unavailable: %s", exc)
        return _error(503, "The news database could not be reached.")
