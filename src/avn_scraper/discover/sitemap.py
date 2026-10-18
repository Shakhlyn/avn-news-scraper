"""XML sitemaps, including Google-News sitemaps (which carry headlines)."""

from __future__ import annotations

import re
import zlib
from datetime import datetime, timedelta, timezone

from lxml import etree

from ..fetch import Fetcher
from ..urls import same_site
from . import Candidate

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)
_MAX_CHILDREN = 4
_MAX_UNDATED = 300
_MAX_XML_BYTES = 50 * 1024 * 1024


def _gunzip(content: bytes) -> bytes:
    """Decompress a .gz sitemap, refusing to inflate past _MAX_XML_BYTES (gzip bombs)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(content, _MAX_XML_BYTES)
    if d.unconsumed_tail:
        raise ValueError("sitemap larger than size limit")
    return out


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _text(el, path: str) -> str | None:
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else None


def parse_sitemap(content: bytes) -> tuple[list[tuple[str, datetime | None]], list[dict]]:
    """Return (child sitemaps [(loc, lastmod)], url entries [{loc,title,published_at}])."""
    if content[:2] == b"\x1f\x8b":
        content = _gunzip(content)
    root = etree.fromstring(content, parser=_PARSER)
    if root is None:
        return [], []
    children = [(_text(sm, "{*}loc"), _parse_dt(_text(sm, "{*}lastmod")))
                for sm in root.iterfind("{*}sitemap")]
    urls = []
    for u in root.iterfind("{*}url"):
        loc = _text(u, "{*}loc")
        if not loc:
            continue
        urls.append({
            "loc": loc,
            "title": _text(u, "{*}news/{*}title") or "",
            "published_at": _parse_dt(_text(u, "{*}news/{*}publication_date") or _text(u, "{*}lastmod")),
        })
    return [c for c in children if c[0]], urls


def _pick_children(children: list[tuple[str, datetime | None]], cutoff: datetime) -> list[str]:
    """From a sitemap index, keep the few child sitemaps likely to hold recent articles."""
    today = datetime.now(timezone.utc)
    date_tokens = {f"{d:%Y-%m-%d}" for d in (today, today - timedelta(days=1))} | {f"{today:%Y/%m}", f"{today:%Y-%m}"}

    def rank(item):
        loc, lastmod = item
        lower = loc.lower()
        score = 0
        if "news" in lower:
            score += 4
        if any(tok in lower for tok in date_tokens):
            score += 3
        if lastmod and lastmod >= cutoff:
            score += 2
        if re.search(r"(image|video|tag|author|categor|page|static)", lower):
            score -= 5
        return score

    ranked = sorted(children, key=lambda c: (rank(c), c[1] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return [loc for loc, _ in ranked[:_MAX_CHILDREN] if rank((loc, _)) > 0] or [loc for loc, _ in ranked[:1]]


async def discover_sitemap(fetcher: Fetcher, url: str, source_id: str, cutoff: datetime,
                           _depth: int = 0) -> list[Candidate]:
    resp = await fetcher.get(url)
    if resp is None:
        return []
    try:
        children, entries = parse_sitemap(resp.content)
    except (etree.XMLSyntaxError, OSError, ValueError, zlib.error):
        return []
    out: list[Candidate] = []
    children = [c for c in children if same_site(c[0], url)]
    if children and _depth < 2:
        for child in _pick_children(children, cutoff):
            out += await discover_sitemap(fetcher, child, source_id, cutoff, _depth + 1)
    # Large undated sitemaps are archives (oldest first); their entries can't be
    # told apart from new ones, so only small undated sitemaps are used.
    keep_undated = len(entries) <= _MAX_UNDATED
    for e in entries:
        if e["published_at"] and e["published_at"] < cutoff:
            continue
        if not e["published_at"] and not keep_undated:
            continue
        out.append(Candidate(url=e["loc"], source_id=source_id, title=e["title"],
                             published_at=e["published_at"], via="sitemap"))
    return out
