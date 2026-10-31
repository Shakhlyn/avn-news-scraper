from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import repository as repo
from ..deps import Db, require_reviewing, same_origin

router = APIRouter(prefix="/api", tags=["scraper"])


@router.post("/scrape", status_code=202, dependencies=[Depends(require_reviewing), Depends(same_origin)])
def start(request: Request) -> dict[str, Any]:
    """Scrape now, looking back to the last full run. Follow it on GET /api/scrape."""
    scrapes = request.app.state.scrapes
    if not scrapes.start("manual"):
        raise HTTPException(409, "A scrape is already running.")
    return scrapes.state


@router.get("/scrape")
def status(request: Request) -> dict[str, Any]:
    return request.app.state.scrapes.state


@router.get("/runs")
def runs(limit: Annotated[int, Query(ge=1, le=100)] = 20, conn: psycopg.Connection = Db) -> list[dict[str, Any]]:
    return repo.list_runs(conn, limit)


@router.get("/health")
def health(conn: psycopg.Connection = Db) -> dict[str, str]:
    conn.execute("SELECT 1")
    return {"status": "ok"}
