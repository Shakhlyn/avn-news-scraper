"""Article text + metadata extraction from HTML."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser

_DATE_META = (
    'meta[property="article:published_time"]',
    'meta[name="article:published_time"]',
    'meta[itemprop="datePublished"]',
    'meta[name="publish-date"]',
    'meta[name="pubdate"]',
    'meta[property="og:published_time"]',
    'meta[name="date"]',
)


@dataclass
class Extracted:
    title: str
    body: str
    summary: str
    author: str
    published_at: datetime | None
    canonical_url: str | None
    image: str | None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    for candidate in (value, value[:19], value[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _meta_date(tree: HTMLParser) -> datetime | None:
    for selector in _DATE_META:
        node = tree.css_first(selector)
        if node is not None:
            dt = _parse_dt(node.attributes.get("content"))
            if dt:
                return dt
    time_node = tree.css_first("time[datetime]")
    return _parse_dt(time_node.attributes.get("datetime")) if time_node is not None else None


def extract(html: str, url: str) -> Extracted:
    tree = HTMLParser(html)
    doc = trafilatura.bare_extraction(html, url=url, with_metadata=True,
                                      include_comments=False, favor_recall=True)
    if doc is not None and hasattr(doc, "as_dict"):
        doc = doc.as_dict()
    doc = doc or {}

    canonical = tree.css_first('link[rel="canonical"]')
    og_title = tree.css_first('meta[property="og:title"]')
    title = doc.get("title") or (og_title.attributes.get("content") if og_title is not None else "") or ""

    return Extracted(
        title=title.strip(),
        body=(doc.get("text") or "").strip(),
        summary=(doc.get("description") or "").strip(),
        author=(doc.get("author") or "").strip(),
        published_at=_meta_date(tree) or _parse_dt(doc.get("date")),
        canonical_url=canonical.attributes.get("href") if canonical is not None else None,
        image=doc.get("image"),
    )
