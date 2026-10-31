from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ScrapeState(BaseModel):
    running: bool
    trigger: str | None
    started_at: str | None
    finished_at: str | None
    ok: bool | None
    error: str | None
    outcome: dict[str, Any] | None


class Run(BaseModel):
    run_id: str
    started_at: datetime | None
    finished_at: datetime | None
    outcome: dict[str, Any] | None
    only_sources: list[str] | None
