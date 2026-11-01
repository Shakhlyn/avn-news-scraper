"""RSS / Atom feeds."""

from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timezone

import feedparser
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..fetch import Fetcher
from . import Candidate


def _strip_html(text: str) -> str:
    if not text or "<" not in text:
        return text or ""
    return HTMLParser(text).text(separator=" ").strip()


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def parse_feed(content: bytes, source_id: str) -> list[Candidate]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        link = e.get("link")
        if not link:
            continue
        out.append(Candidate(
            url=link,
            source_id=source_id,
            title=_strip_html(e.get("title", "")),
            summary=_strip_html(e.get("summary", ""))[:1000],
            published_at=_entry_time(e),
            via="rss",
        ))
    return out


def looks_like_feed(content: bytes) -> bool:
    head = content[:500].lstrip().lower()
    return head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head


async def discover_feed(fetcher: Fetcher, url: str, source_id: str) -> list[Candidate]:
    resp = await fetcher.get(url)
    if resp is None or not looks_like_feed(resp.content):
        return []
    return await asyncio.to_thread(parse_feed, resp.content, source_id)
