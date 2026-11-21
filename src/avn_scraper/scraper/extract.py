"""Article text + metadata extraction from HTML."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .normalize import normalize, normalize_unicode

# Publication-date meta tags, by name or property. Modified/updated dates are left out on
# purpose: an old story edited today must not pass the 24-hour window.
_DATE_META_NAMES = (
    "article:published_time", "og:published_time", "datepublished", "published_time",
    "published_date", "publish-date", "publish_date", "publishdate", "publication_date",
    "pubdate", "pub_date", "publishtime", "parsely-pub-date", "sailthru.date",
    "dcterms.created", "dc.date.issued", "dc.date", "date", "cxenseparse:publishtime",
    "original-publish-date", "article_date_original",
)
# schema.org types whose datePublished is the story's own date (a WebPage's may differ).
_ARTICLE_TYPES = re.compile(r"article|posting|report", re.I)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TITLE_SEPARATORS = (" | ", " - ", " – ", " — ", " :: ", " » ", " ; ")
_TITLE_FILTER = re.compile(r"[\W_]+")
_MIN_H1_WORDS = 2


@dataclass
class Extracted:
    title: str
    body: str
    summary: str
    author: str
    published_at: datetime | None
    canonical_url: str | None
    image: str | None
    # True when the page gave only a day (no time). Then published_at is midnight UTC of
    # that day and must be compared by day, not by the hour.
    date_only: bool = False


def _parse_dt(value: str | None) -> datetime | None:
    """ISO 8601 or RFC 2822 date string -> aware datetime (UTC when no offset is given)."""
    if not value or not isinstance(value, str):
        return None
    value = normalize_unicode(value).strip()
    if not value:
        return None
    candidates = (value.replace("Z", "+00:00"), value[:10])
    dt = None
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if dt is None:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ld_items(tree: HTMLParser) -> Iterator[dict]:
    """Every object in the page's JSON-LD blocks, with @graph and lists flattened."""
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text(), strict=False)
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                if isinstance(item.get("@graph"), list):
                    stack.extend(item["@graph"])


def _is_article(item: dict) -> bool:
    kind = item.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and _ARTICLE_TYPES.search(k) for k in kinds)


def _date_candidates(tree: HTMLParser, ld: list[dict]) -> Iterator[str]:
    """Raw publication-date strings, most reliable first."""
    for item in sorted(ld, key=lambda i: not _is_article(i)):
        if isinstance(item.get("datePublished"), str):
            yield item["datePublished"]
    for item in ld:
        if _is_article(item) and isinstance(item.get("dateCreated"), str):
            yield item["dateCreated"]
    metas = {}
    for meta in tree.css("meta[content]"):
        key = (meta.attributes.get("property") or meta.attributes.get("name")
               or meta.attributes.get("itemprop") or "").strip().lower()
        if key in _DATE_META_NAMES and key not in metas:
            metas[key] = meta.attributes.get("content")
    for name in _DATE_META_NAMES:
        if metas.get(name):
            yield metas[name]
    for node in tree.css('[itemprop="datePublished"]'):
        yield node.attributes.get("datetime") or node.attributes.get("content") or ""
    for node in tree.css("time[pubdate], article time[datetime], header time[datetime]"):
        yield node.attributes.get("datetime") or ""


def _published(tree: HTMLParser, ld: list[dict], fallback: str | None) -> tuple[datetime | None, bool]:
    """(published_at, date_only). A full timestamp beats a bare day from a better source,
    because a bare day read as midnight can push yesterday's story out of the window."""
    day: datetime | None = None
    for raw in _date_candidates(tree, ld):
        dt = _parse_dt(raw)
        if dt is None:
            continue
        if not _DATE_ONLY.match(normalize_unicode(raw).strip()):
            return dt, False
        day = day or dt
    if day is not None:
        return day, True
    # Last resorts: the first <time> anywhere on the page, then trafilatura's guess.
    time_node = tree.css_first("time[datetime]")
    for raw in (time_node.attributes.get("datetime") if time_node is not None else None, fallback):
        dt = _parse_dt(raw)
        if dt is not None:
            return dt, bool(_DATE_ONLY.match(normalize_unicode(raw).strip()))
    return None, False


def _paragraph_text(html_or_node) -> str:
    tree = HTMLParser(html_or_node) if isinstance(html_or_node, str) else html_or_node
    paragraphs = [" ".join(p.text().split()) for p in tree.css("p")]
    return "\n".join(p for p in paragraphs if p)


def _ld_article_body(ld: list[dict]) -> str:
    """Longest schema.org articleBody in the page's JSON-LD (most news CMSs publish one)."""
    best = ""
    for item in ld:
        body = item.get("articleBody")
        if isinstance(body, str) and len(body) > len(best):
            best = body
    if "<" in best:
        best = HTMLParser(best).text(separator="\n")
    return best.strip()


def _itemprop_body(tree: HTMLParser) -> str:
    """Paragraphs inside the element marked itemprop="articleBody"."""
    best = ""
    for node in tree.css('[itemprop="articleBody"]'):
        text = _paragraph_text(node)
        if len(text) > len(best):
            best = text
    return best


def _key(text: str) -> str:
    return _TITLE_FILTER.sub("", normalize(text))


def _site_key(tree: HTMLParser) -> str:
    site = tree.css_first('meta[property="og:site_name"]')
    return _key(site.attributes.get("content") or "") if site is not None else ""


def _clean_title(title: str, tree: HTMLParser) -> str:
    """Drop the outlet name that <title>/og:title often carry ("Headline | Prothom Alo").

    When the title splits at a separator and its longer side is the page's <h1>, that
    <h1> is the title. Otherwise a part is dropped only when it is og:site_name.
    """
    site_key = _site_key(tree)
    h1s = {_key(h): h for h in (" ".join(n.text(separator=" ").split()) for n in tree.css("h1"))
           if len(h.split()) >= _MIN_H1_WORDS and _key(h) and _key(h) != site_key}
    # The headline is the longer side of the split; a logo <h1> ("Prothom Alo") is the shorter.
    for sep in _TITLE_SEPARATORS:
        parts = title.split(sep)
        for i in range(1, len(parts)):
            left, right = sep.join(parts[:i]).strip(), sep.join(parts[i:]).strip()
            longer = left if len(left) >= len(right) else right
            if _key(longer) in h1s:
                return h1s[_key(longer)]
    if site_key:
        for sep in _TITLE_SEPARATORS:
            parts = [p.strip() for p in title.split(sep)]
            kept = [p for p in parts if _key(p) != site_key]
            if len(parts) > 1 and kept and len(kept) < len(parts):
                return sep.join(kept)
    return title


def _pick_title(guess: str | None, tree: HTMLParser) -> str:
    """trafilatura's title, unless it is only the outlet name; then og:title or <title>."""
    site_key = _site_key(tree)
    og = tree.css_first('meta[property="og:title"]')
    tag = tree.css_first("title")
    for title in (guess, og.attributes.get("content") if og is not None else None,
                  tag.text() if tag is not None else None):
        title = " ".join((title or "").split())
        if title and _key(title) != site_key:
            return _clean_title(title, tree).strip()
    return " ".join((guess or "").split())


def extract(html: str, url: str) -> Extracted:
    tree = HTMLParser(html)
    ld = list(_ld_items(tree))
    doc = trafilatura.bare_extraction(html, url=url, with_metadata=True,
                                      include_comments=False, favor_recall=True)
    if doc is not None and hasattr(doc, "as_dict"):
        doc = doc.as_dict()
    doc = doc or {}

    body = (doc.get("text") or "").strip()
    # trafilatura sometimes keeps only part of the story; prefer the publisher's own
    # full text (JSON-LD articleBody, or the itemprop="articleBody" element) when it is
    # clearly longer.
    for alt in (_ld_article_body(ld), _itemprop_body(tree)):
        if len(alt) > 1.2 * len(body):
            body = alt

    canonical = tree.css_first('link[rel="canonical"]')
    title = _pick_title(doc.get("title"), tree)
    published, date_only = _published(tree, ld, doc.get("date"))

    return Extracted(
        title=title,
        body=body,
        summary=(doc.get("description") or "").strip(),
        author=(doc.get("author") or "").strip(),
        published_at=published,
        canonical_url=canonical.attributes.get("href") if canonical is not None else None,
        image=doc.get("image"),
        date_only=date_only,
    )
