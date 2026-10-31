from __future__ import annotations

from datetime import date
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request

from .. import repository as repo
from ..deps import Db

router = APIRouter(prefix="/api", tags=["news"])


@router.get("/news")
def news(
    request: Request,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    source: Annotated[str, Query(max_length=100)] = "",
    topic: Annotated[str, Query(max_length=100)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    conn: psycopg.Connection = Db,
) -> dict[str, Any]:
    """Stories, widest reported first. Unchecked, so on the review desk only."""
    if not request.app.state.reviewing:
        raise HTTPException(404, "The unchecked news is on the review desk only.")
    filters = repo.NewsFilters(start, end, source, topic, q.strip(), page, limit)
    return repo.list_news(conn, filters, request.app.state.category_order)
