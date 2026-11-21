"""Finding candidate article URLs (with headline/summary when available)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Candidate:
    url: str
    source_id: str
    title: str = ""
    summary: str = ""
    published_at: datetime | None = None  # timezone-aware UTC when known
    via: str = ""  # rss | sitemap | listing | catchall
    # (html, Extracted) when discovery already downloaded the article (date probe), so the
    # pipeline does not download it again.
    prefetched: tuple[str, Any] | None = field(default=None, repr=False, compare=False)
