"""Headline links on a homepage or section page."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..fetch import Fetcher
from ..urls import same_site
from . import Candidate

_MIN_HEADLINE_CHARS = 12
_SKIP_PATH = re.compile(r"/(tag|tags|topic|author|category|video|videos|photo|gallery|page|search|login)(/|$)", re.I)


def parse_listing(html: str, page_url: str, homepage: str, source_id: str) -> list[Candidate]:
    tree = HTMLParser(html)
    seen: dict[str, Candidate] = {}
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(page_url, href)
        if not url.startswith("http") or not same_site(url, homepage) or _SKIP_PATH.search(url):
            continue
        text = " ".join(a.text(separator=" ").split())
        if len(text) < _MIN_HEADLINE_CHARS:
            continue
        # Prefer the longest anchor text seen for a URL (image links have none).
        if url not in seen or len(text) > len(seen[url].title):
            seen[url] = Candidate(url=url, source_id=source_id, title=text, via="listing")
    return list(seen.values())


def find_feed_links(html: str, page_url: str) -> list[str]:
    """<link rel="alternate" type="application/rss+xml"> declarations on a page."""
    tree = HTMLParser(html)
    out = []
    for link in tree.css("link[rel=alternate]"):
        kind = (link.attributes.get("type") or "").lower()
        href = link.attributes.get("href")
        if href and ("rss" in kind or "atom" in kind):
            out.append(urljoin(page_url, href))
    return out


async def discover_listing(fetcher: Fetcher, url: str, homepage: str, source_id: str) -> tuple[list[Candidate], str]:
    """Return (candidates, html) so the caller can reuse the page for feed discovery."""
    resp = await fetcher.get(url)
    if resp is None:
        return [], ""
    return await asyncio.to_thread(parse_listing, resp.text, str(resp.url), homepage, source_id), resp.text
