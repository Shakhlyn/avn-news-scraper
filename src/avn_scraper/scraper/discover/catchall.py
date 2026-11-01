"""Optional search-engine news feed that catches outlets not listed in sources.yaml."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, quote_plus, urlsplit

from ..fetch import Fetcher
from ..urls import host_of
from . import Candidate
from .rss import parse_feed

BING_URL = "https://www.bing.com/news/search?q={q}&format=rss&cc=BD&qft=sortbydate%3d%221%22"


def unwrap_bing_link(link: str) -> str:
    """Bing wraps results as apiclick.aspx?...&url=<real url>."""
    parts = urlsplit(link)
    host = (parts.hostname or "").lower()
    if host == "bing.com" or host.endswith(".bing.com"):
        target = parse_qs(parts.query).get("url")
        if target and urlsplit(target[0]).scheme in ("http", "https"):
            return target[0]
    return link


async def discover_catchall(fetcher: Fetcher, queries: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    for q in queries:
        resp = await fetcher.get(BING_URL.format(q=quote_plus(q)))
        if resp is None:
            continue
        for c in await asyncio.to_thread(parse_feed, resp.content, source_id=""):
            c.url = unwrap_bing_link(c.url)
            c.source_id = "web:" + host_of(c.url)
            c.via = "catchall"
            out.append(c)
    return out
