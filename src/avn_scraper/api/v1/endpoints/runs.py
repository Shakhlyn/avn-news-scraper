from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ....repositories import runs as repo
from ....schemas.scrape import Run
from ...deps import Db

router = APIRouter(prefix="/runs", tags=["scraper"])


@router.get("", response_model=list[Run])
async def list_runs(conn: Db, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return await repo.list_runs(conn, limit)
