from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ....core.limiter import SCRAPE_LIMIT, limiter
from ....core.security import require_reviewing, same_origin
from ....schemas.scrape import ScrapeState
from ...deps import Scrapes

router = APIRouter(prefix="/scrape", tags=["scraper"])


@router.post("", status_code=202, response_model=ScrapeState,
             dependencies=[Depends(require_reviewing), Depends(same_origin)])
@limiter.limit(SCRAPE_LIMIT)
def start_scrape(request: Request, scrapes: Scrapes):
    """Scrape now, looking back to the last full run. Follow it on GET /scrape."""
    if not scrapes.start("manual"):
        raise HTTPException(409, "A scrape is already running.")
    return scrapes.state


@router.get("", response_model=ScrapeState)
def scrape_status(scrapes: Scrapes):
    return scrapes.state
