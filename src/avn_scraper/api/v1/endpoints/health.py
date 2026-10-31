from __future__ import annotations

from fastapi import APIRouter, Request

from ....core.limiter import limiter
from ...deps import Db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
@limiter.exempt
def health(request: Request, conn: Db) -> dict[str, str]:
    conn.execute("SELECT 1")
    return {"status": "ok"}
