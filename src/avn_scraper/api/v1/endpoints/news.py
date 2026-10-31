from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ....repositories import news as repo
from ....schemas.news import NewsPage
from ...deps import CategoryOrder, CurrentSettings, Db

router = APIRouter(prefix="/news", tags=["news"])


@router.get("", response_model=NewsPage)
def list_news(
    conn: Db,
    settings: CurrentSettings,
    order: CategoryOrder,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    source: Annotated[str, Query(max_length=100)] = "",
    topic: Annotated[str, Query(max_length=100)] = "",
    q: Annotated[str, Query(max_length=200)] = "",
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    """Stories, widest reported first. Unchecked, so on the review desk only."""
    if not settings.reviewing:
        raise HTTPException(404, "The unchecked news is on the review desk only.")
    return repo.list_news(conn, repo.NewsFilters(start, end, source, topic, q.strip(), page, limit), order)
