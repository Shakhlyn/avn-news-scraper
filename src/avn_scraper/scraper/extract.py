"""Article text + metadata extraction from HTML."""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import trafilatura
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .normalize import normalize, normalize_unicode

# Publication-date meta tags, by name or property. Modified/updated dates are left out on
# purpose: an old story edited today must not pass the run's window.
_DATE_META_NAMES = (
    "article:published_time", "og:published_time", "datepublished", "published_time",
    "published_date", "publish-date", "publish_date", "publishdate", "publication_date",
    "pubdate", "pub_date", "publishtime", "parsely-pub-date", "sailthru.date",
    "dcterms.created", "dc.date.issued", "dc.date", "date", "cxenseparse:publishtime",
    "original-publish-date", "article_date_original",
)
# schema.org types whose datePublished is the story's own date (a WebPage's may differ).
_ARTICLE_TYPES = re.compile(r"article|posting|report", re.I)
# Every outlet is in Bangladesh: a time without an offset is Dhaka wall-clock time.
_DHAKA = ZoneInfo("Asia/Dhaka")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TITLE_SEPARATORS = (" | ", " - ", " – ", " — ", " :: ", " » ", " ; ")
_TITLE_FILTER = re.compile(r"[\W_]+")
_MIN_H1_WORDS = 2
# Real headlines are far shorter; a longer title is left as is (and cut) instead of split.
_MAX_TITLE_CHARS = 500

# Visible dates, for pages with no date markup (many local outlets): "December 24, 2024",
# "২৪ ডিসেম্বর ২০২৪", "৩ ঘণ্টা আগে". Only in elements that look like the article's meta line,
# never in a header clock ("today-date", "time-now") or an "updated" stamp.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
    "জানুয়ারি": 1, "জানুয়ারী": 1, "ফেব্রুয়ারি": 2, "ফেব্রুয়ারী": 2, "মার্চ": 3, "এপ্রিল": 4,
    "মে": 5, "জুন": 6, "জুলাই": 7, "আগস্ট": 8, "আগষ্ট": 8, "সেপ্টেম্বর": 9, "অক্টোবর": 10,
    "নভেম্বর": 11, "ডিসেম্বর": 12,
}
_MONTH_RE = "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True))
_DAY_MONTH_YEAR = re.compile(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s*({_MONTH_RE})\.?,?\s*(\d{{4}})", re.I)
_MONTH_DAY_YEAR = re.compile(rf"(?<![^\W\d_])({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})", re.I)
_AGO = re.compile(r"(?<!\d)(\d{1,4})\s*(মিনিট|ঘণ্টা|ঘন্টা|দিন|সপ্তাহ|মাস|বছর|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)"
                  r"\s*(?:আগে|পূর্বে|ago)", re.I)
_AGO_UNITS = {"মিনিট": "m", "ঘণ্টা": "h", "ঘন্টা": "h", "দিন": "d", "সপ্তাহ": "w", "মাস": "mo", "বছর": "y"}
_META_ATTR = re.compile(r"date|time|meta|publish|posted|byline|entry-?info|post-?info", re.I)
_NOT_META_ATTR = re.compile(r"today|now|current|clock|update|modif|weather|top[-_]?bar|site[-_]?header|ticker|masthead", re.I)
# The same, minus "update": the last-resort pass. Some local-news WordPress themes (mkantho's)
# print the only date as "Update Time : 08:10 am, Sunday, 1 November 2026".
_NOT_META_ATTR_LAST = re.compile(r"today|now|current|clock|modif|weather|top[-_]?bar|site[-_]?header|ticker|masthead", re.I)
# A labelled publish line ("প্রকাশিত: ২৭/১০/২০২৬", "Published: 27-10-2026") is the article's
# own date whatever its element is called. Numeric dates are day first, as Bangladeshi sites write them.
_PUBLISH_LABEL = r"(?:প্রকাশিত|প্রকাশের\s*সময়|প্রকাশকাল|প্রকাশ|published(?:\s*(?:on|at|date|time))?|posted(?:\s*on)?|publish\s*(?:date|time))"
_LABELED = re.compile(rf"{_PUBLISH_LABEL}\s*[:：]?", re.I)
_LABELED_NUMERIC = re.compile(rf"{_PUBLISH_LABEL}\s*[:：]?\s*(?:[^\d\s]+\s*,?\s*)?(\d{{1,2}})[/.\-](\d{{1,2}})[/.\-](\d{{4}})", re.I)


@dataclass
class Extracted:
    title: str
    body: str
    summary: str
    author: str
    published_at: datetime | None
    canonical_url: str | None
    image: str | None
    # True when the page gave only a day (no time, or exactly 00:00:00). Then published_at
    # is midnight of that day and must be compared by day, not by the hour.
    date_only: bool = False


def _parse_dt(value: str | None) -> datetime | None:
    """ISO 8601 or RFC 2822 date string -> aware datetime (Dhaka time when no offset is given)."""
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
    return dt if dt.tzinfo else dt.replace(tzinfo=_DHAKA)


def _bare_day(raw: str, dt: datetime) -> bool:
    """A day without a time: "2026-11-20", or a timestamp of exactly midnight."""
    return bool(_DATE_ONLY.match(normalize_unicode(raw).strip())) or (
        dt.hour == dt.minute == dt.second == dt.microsecond == 0)


def _ld_items(tree: HTMLParser) -> Iterator[dict]:
    """Every object in the page's JSON-LD blocks, with @graph and lists flattened."""
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text(), strict=False)
        except ValueError:
            continue
        stack = deque([data])
        while stack:
            item = stack.popleft()
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


def _is_template(item: dict, site_key: str) -> bool:
    """A site-wide JSON-LD block (same on every page, often with a stale date): its page
    is the homepage, or its headline is the outlet name."""
    page = item.get("mainEntityOfPage")
    page = page.get("@id") if isinstance(page, dict) else page
    for url in (page, item.get("url")):
        if isinstance(url, str) and url.startswith("http") and urlsplit(url).path in ("", "/"):
            return True
    headline = item.get("headline")
    return bool(site_key) and isinstance(headline, str) and _key(headline) == site_key


def _date_candidates(tree: HTMLParser, ld: list[dict]) -> Iterator[str]:
    """Raw publication-date strings, most reliable first."""
    site_key = _site_key(tree)
    ld = [item for item in ld if not _is_template(item, site_key)]
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


def _ago(amount: int, unit: str, now: datetime) -> datetime:
    unit = _AGO_UNITS.get(unit, unit.lower())
    if unit.startswith("mo"):
        return now - timedelta(days=30 * amount)
    if unit.startswith("mi") or unit == "m":
        return now - timedelta(minutes=amount)
    step = {"h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(weeks=1), "y": timedelta(days=365)}
    return now - step[unit[0]] * amount


def _text_to_day(text: str, now: datetime) -> datetime | None:
    """The first date written out in text, as midnight (Dhaka) of that day."""
    text = normalize_unicode(text)
    day = month = year = None
    if m := _DAY_MONTH_YEAR.search(text):
        day, month, year = int(m[1]), _MONTHS[m[2].lower()], int(m[3])
    elif m := _MONTH_DAY_YEAR.search(text):
        day, month, year = int(m[2]), _MONTHS[m[1].lower()], int(m[3])
    elif m := _LABELED_NUMERIC.search(text):
        day, month, year = int(m[1]), int(m[2]), int(m[3])
    elif m := _AGO.search(text):
        when = _ago(int(m[1]), m[2], now).astimezone(_DHAKA)
        day, month, year = when.day, when.month, when.year
    if day is None:
        return None
    try:
        dt = datetime(year, month, day, tzinfo=_DHAKA)
    except ValueError:
        return None
    return dt if 2000 <= year and dt <= now + timedelta(days=1) else None


def _meta_like(node, depth: int = 3, last_resort: bool = False) -> bool:
    """The element (or a close parent) is named like a date / meta line, and none of them
    like a clock or (except on the last-resort pass) an "updated" stamp."""
    meta = False
    reject = _NOT_META_ATTR_LAST if last_resort else _NOT_META_ATTR
    while node is not None and depth >= 0:
        names = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        if reject.search(names):
            return False
        meta = meta or bool(_META_ATTR.search(names))
        node, depth = node.parent, depth - 1
    return meta


_NEAR_HEADLINE_CHARS = 1500
_MAX_META_CHARS = 300  # a meta line is short; longer text is body copy, and capping it keeps the regexes cheap


def _date_in(node, now: datetime, last_resort: bool = False) -> datetime | None:
    if node.tag in ("script", "style", "-text"):
        return None
    own = node.text(deep=False)
    if not own or not own.strip() or len(own) > _MAX_META_CHARS:
        return None
    if _LABELED.search(normalize_unicode(own)) or _meta_like(node, last_resort=last_resort):
        return _text_to_day(own, now)
    return None


def _text_date(tree: HTMLParser, now: datetime) -> datetime | None:
    """A visible date in the article's meta line, for pages without date markup.

    Only next to the headline: in the <h1>'s own header block, or in the text right after it.
    Dates earlier on the page are the masthead's "today" or other stories.
    """
    site_key = _site_key(tree)
    h1s = [h for h in tree.css("h1") if len(h.text().split()) >= _MIN_H1_WORDS and _key(h.text()) != site_key]
    if not h1s:
        return None
    headline = max(h1s, key=lambda h: len(h.text()))
    # An "updated" stamp only when nothing else is near the headline: it is never earlier than
    # the publish date, so at worst an old story edited later looks newer.
    for last_resort in (False, True):
        if (dt := _near_headline(tree, headline, now, last_resort)) is not None:
            return dt
    return None


def _near_headline(tree: HTMLParser, headline, now: datetime, last_resort: bool) -> datetime | None:
    block, level = headline.parent, 0
    while block is not None and block.tag not in ("body", "html") and level < 2:
        for node in block.traverse():
            if (dt := _date_in(node, now, last_resort)) is not None:
                return dt
        block, level = block.parent, level + 1
    root = tree.body or tree.root
    after, chars = False, 0
    for node in root.traverse():
        if node is headline or node.mem_id == headline.mem_id:
            after = True
            continue
        if not after:
            continue
        if (dt := _date_in(node, now, last_resort)) is not None:
            return dt
        own = node.text(deep=False) if node.tag not in ("script", "style") else ""
        chars += len(own.strip()) if own else 0
        if chars > _NEAR_HEADLINE_CHARS:
            break
    return None


def _published(tree: HTMLParser, ld: list[dict], fallback: str | None,
               now: datetime | None = None) -> tuple[datetime | None, bool]:
    """(published_at, date_only). A full timestamp beats a bare day from a better source,
    because a bare day read as midnight can push yesterday's story out of the window."""
    day: datetime | None = None
    for raw in _date_candidates(tree, ld):
        dt = _parse_dt(raw)
        if dt is None:
            continue
        if not _bare_day(raw, dt):
            return dt, False
        day = day or dt
    if day is not None:
        return day, True
    # Last resorts: the first <time> anywhere on the page, a date written in the article's
    # meta line, then trafilatura's guess (which can pick up the header's "today" date).
    time_node = tree.css_first("time[datetime]")
    raw = time_node.attributes.get("datetime") if time_node is not None else None
    if (dt := _parse_dt(raw)) is not None:
        return dt, _bare_day(raw, dt)
    now = now or datetime.now(_DHAKA)
    if (dt := _text_date(tree, now)) is not None:
        return dt, True
    if (dt := _parse_dt(fallback)) is not None:
        bare = _bare_day(fallback, dt)
        # A bare "today" from the guess is almost always the header clock, which would make every
        # old story look new. No date is safer: a saved article falls back to the scrape date.
        if bare and dt.astimezone(_DHAKA).date() == now.astimezone(_DHAKA).date():
            return None, False
        return dt, bare
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
    if len(title) > _MAX_TITLE_CHARS:
        return title[:_MAX_TITLE_CHARS]
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


def extract(html: str, url: str, now: datetime | None = None) -> Extracted:
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
    published, date_only = _published(tree, ld, doc.get("date"), now)

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
