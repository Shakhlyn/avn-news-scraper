"""Per-outlet discovery: homepage + section tabs + configured or auto-discovered feeds/sitemaps."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Source
from ..fetch import Fetcher
from ..urls import same_site
from . import Candidate
from .listing import discover_listing, find_feed_links
from .rss import discover_feed
from .sections import discover_sections
from .sitemap import discover_sitemap

log = logging.getLogger(__name__)


@dataclass
class SourceResult:
    source_id: str
    candidates: list[Candidate] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    endpoints: dict[str, list[str]] = field(default_factory=dict)
    error: str | None = None

    def add(self, via: str, items: list[Candidate]) -> None:
        self.candidates += items
        self.counts[via] = self.counts.get(via, 0) + len(items)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.candidates)


class EndpointCache:
    """Remembers auto-discovered feed/sitemap URLs per source for a few days."""

    def __init__(self, path: Path, max_age_days: int):
        self.path = path
        self.max_age = timedelta(days=max_age_days)
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def get(self, source_id: str) -> dict | None:
        entry = self.data.get(source_id)
        if not entry:
            return None
        checked = datetime.fromisoformat(entry["checked"])
        return entry if datetime.now(timezone.utc) - checked < self.max_age else None

    def set(self, source_id: str, feeds: list[str], sitemaps: list[str]) -> None:
        self.data[source_id] = {"feeds": feeds, "sitemaps": sitemaps,
                                "checked": datetime.now(timezone.utc).isoformat()}

    def clear(self, source_id: str) -> None:
        self.data.pop(source_id, None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")


class ListingMemory:
    """Article ids seen on section pages per source, so paging stops where the last run reached."""

    def __init__(self, path: Path, keep_days: int = 4):
        self.path = path
        self.keep = timedelta(days=keep_days)
        self.started = round(time.time(), 3)
        try:
            self.data: dict[str, dict[str, float]] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def seen_before(self, source_id: str, ids: set[str]) -> int:
        """How many of ids were first seen by an earlier run."""
        seen = self.data.get(source_id, {})
        return sum(1 for i in ids if i in seen and seen[i] < self.started)

    def record(self, source_id: str, ids: set[str]) -> None:
        entries = self.data.setdefault(source_id, {})
        for i in ids:
            entries.setdefault(i, self.started)

    def save(self) -> None:
        oldest = time.time() - self.keep.total_seconds()
        self.data = {sid: kept for sid, entries in self.data.items()
                     if (kept := {i: t for i, t in entries.items() if t >= oldest})}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")


def _rank_sitemaps(urls: list[str]) -> list[str]:
    def score(u: str) -> int:
        lower = u.lower()
        return (4 if "news" in lower else 0) - (5 if any(x in lower for x in ("image", "video", "tag", "author", "categor")) else 0)
    return [u for u in sorted(urls, key=score, reverse=True) if score(u) >= 0][:2]


async def discover_source(fetcher: Fetcher, source: Source, cutoff: datetime, cache: EndpointCache,
                          sections_settings: dict | None = None,
                          memory: ListingMemory | None = None) -> SourceResult:
    result = SourceResult(source.id)
    try:
        items, homepage_html = await discover_listing(fetcher, source.homepage, source.homepage, source.id)
        result.add("listing", items)
        items, sections, pages = await discover_sections(fetcher, source, homepage_html, cutoff,
                                                         sections_settings, memory)
        result.add("section", items)
        result.counts["section_pages"] = pages

        feeds, sitemaps = list(source.feeds), list(source.sitemaps)
        if not feeds and not sitemaps:
            cached = cache.get(source.id)
            if cached is not None:
                feeds, sitemaps = cached["feeds"], cached["sitemaps"]
            else:
                # Only the outlet's own feeds/sitemaps: pages can point anywhere.
                links = await asyncio.to_thread(find_feed_links, homepage_html, source.homepage) if homepage_html else []
                feeds = [u for u in links if "comment" not in u.lower() and same_site(u, source.homepage)][:2]
                robots = await fetcher.robots_sitemaps(source.homepage)
                sitemaps = _rank_sitemaps([u for u in robots if same_site(u, source.homepage)])
                if homepage_html:  # only cache when the site actually answered
                    cache.set(source.id, feeds, sitemaps)
        result.endpoints = {"feeds": feeds, "sitemaps": sitemaps, "sections": sections}

        for url in feeds:
            result.add("rss", await discover_feed(fetcher, url, source.id))
        for url in sitemaps:
            result.add("sitemap", await discover_sitemap(fetcher, url, source.id, cutoff))
    except Exception as exc:  # one broken site must not stop the run
        log.warning("discovery failed for %s: %r", source.id, exc)
        result.error = repr(exc)
    return result
