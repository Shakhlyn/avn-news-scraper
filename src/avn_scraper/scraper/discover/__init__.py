"""Finding candidate article URLs (with headline/summary when available)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Candidate:
    url: str
    source_id: str
    title: str = ""
    summary: str = ""
    published_at: datetime | None = None  # timezone-aware UTC when known
    via: str = ""  # rss | sitemap | listing | catchall
