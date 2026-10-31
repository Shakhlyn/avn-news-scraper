"""Article text + metadata extraction from HTML."""

from __future__ import annotations

import json
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


def _ld_article_body(tree: HTMLParser) -> str:
    """Longest schema.org articleBody in the page's JSON-LD (most news CMSs publish one)."""
    best = ""
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text(), strict=False)
        except ValueError:
            continue
        if isinstance(data, dict):
            data = data.get("@graph", [data])
        for item in data if isinstance(data, list) else []:
            body = item.get("articleBody") if isinstance(item, dict) else None
            if isinstance(body, str) and len(body) > len(best):
                best = body
    if "<" in best:
        best = HTMLParser(best).text(separator="\n")
    return best.strip()


def extract(html: str, url: str) -> Extracted:
    tree = HTMLParser(html)
    doc = trafilatura.bare_extraction(html, url=url, with_metadata=True,
                                      include_comments=False, favor_recall=True)
    if doc is not None and hasattr(doc, "as_dict"):
        doc = doc.as_dict()
    doc = doc or {}

    body = (doc.get("text") or "").strip()
    # trafilatura sometimes keeps only part of the story; prefer the publisher's own
    # full text when it is clearly longer.
    ld_body = _ld_article_body(tree)
    if len(ld_body) > 1.2 * len(body):
        body = ld_body

    canonical = tree.css_first('link[rel="canonical"]')
    og_title = tree.css_first('meta[property="og:title"]')
    title = doc.get("title") or (og_title.attributes.get("content") if og_title is not None else "") or ""

    return Extracted(
        title=title.strip(),
        body=body,
        summary=(doc.get("description") or "").strip(),
        author=(doc.get("author") or "").strip(),
        published_at=_meta_date(tree) or _parse_dt(doc.get("date")),
        canonical_url=canonical.attributes.get("href") if canonical is not None else None,
        image=doc.get("image"),
    )
