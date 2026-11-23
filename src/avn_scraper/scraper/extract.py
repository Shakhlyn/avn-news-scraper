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
_SAMAY = "(?:সম(?:য়|য়)|তারিখ)"  # সময় (either spelling of য়) or তারিখ
_PUBLISH_LABEL = (r"(?:প্রকাশিত|প্রকাশের\s*" + _SAMAY + r"|প্রকাশকাল|প্রকাশ"
                  # "Uploaded" is the publish time on some local-news themes (sunamkantha,
                  # somoyersomikoron: "আপলোড সময় : ২২-১১-২০২৬", "আপলোড তারিখঃ ১২-০৯-২০২৬ ইং").
                  r"|আপলোড(?:\s*" + _SAMAY + r")?|uploaded?(?:\s*(?:on|at|date|time))?"
                  r"|published(?:\s*(?:on|at|date|time))?|posted(?:\s*on)?|publish\s*(?:date|time))")
# "Updated" lines: only on the last-resort pass, when nothing else is near the headline (5.2).
_UPDATE_LABEL = (r"(?:আপডেট(?:\s*করা\s*হ\S*)?(?:\s*" + _SAMAY + r")?|সর্বশেষ\s*আপডেট"
                 r"|updated?(?:\s*(?:on|at|date|time))?|last\s*updated?)")
_COLON = r"\s*[:：ঃ]?"
# After a label: day-first "২২-১১-২০২৬" (groups 1-3) or year-first "২০২৪-০২-১৬" (groups 4-6).
_NUMERIC_AFTER = (r"\s*(?:[^\d\s]+\s*,?\s*)?(?:(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})"
                  r"|(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2}))(?!\d)")
_LABELED = re.compile(_PUBLISH_LABEL + _COLON, re.I)
_LABELED_NUMERIC = re.compile(_PUBLISH_LABEL + _COLON + _NUMERIC_AFTER, re.I)
# The label after the date: "১১-০৬-২০২২ ০১:১০ অপরাহ্ন প্রকাশিত হয়েছে" (sirajganjkantho).
_NUMERIC_THEN_LABEL = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?!\d)"
                                 r"(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?[^\d]{0,30}?"
                                 r"(?:প্রকাশিত|প্রকাশ|published|posted)", re.I)
_UPDATED = re.compile(_UPDATE_LABEL + _COLON, re.I)
_UPDATED_NUMERIC = re.compile(_UPDATE_LABEL + _COLON + _NUMERIC_AFTER, re.I)
# A date written out in a date field (JSON-LD / meta), as Bangla sites sometimes fill them:
# "১৭:২৫, ২২ নভেম্বর ২০২৬", "২২-১১-২০২৬ ০৩:১৫:৫০ পূর্বাহ্ন". Numeric dates are day first.
_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?!\d)")
_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?\s*"
                    r"(am|pm|a\.m\.|p\.m\.|এএম|পিএম|পূর্বাহ্ন|অপরাহ্ন|সকাল|ভোর|দুপুর|বিকাল|বিকেল|সন্ধ্যা|রাত)?", re.I)
_AM = {"am", "a.m.", "এএম", "পূর্বাহ্ন", "সকাল", "ভোর"}
_PM = {"pm", "p.m.", "পিএম", "অপরাহ্ন", "দুপুর", "বিকাল", "বিকেল", "সন্ধ্যা"}


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
            return _written_dt(value)
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


def _headline_scope(headline, article: bool = False):
    """The part of the page that belongs to the story: the headline's block 3 levels up, or
    (article=True) the <article> holding the headline. None when there is no headline."""
    if headline is None:
        return None
    if article:
        up = headline.parent
        while up is not None:
            if up.tag == "article":
                return up
            up = up.parent
        return None
    block = headline
    for _ in range(3):
        if block.parent is None or block.parent.tag in ("body", "html"):
            break
        block = block.parent
    return block


# Other stories' teasers next to the article: their <time>s are not the story's date.
_TEASER_ATTR = re.compile(r"card|teaser|related|widget|sidebar|latest|popular|more-?news|read-?more|recommend", re.I)
_TEASER_DEPTH = 5


def _in_teaser(node) -> bool:
    up, depth = node.parent, 0
    while up is not None and depth < _TEASER_DEPTH:
        if _TEASER_ATTR.search(f"{up.attributes.get('class') or ''} {up.attributes.get('id') or ''}"):
            return True
        up, depth = up.parent, depth + 1
    return False


def _times_near(headline, scope) -> Iterator[str]:
    """datetime values of the <time>s in scope outside teaser cards, closest to the headline first."""
    order = {n.mem_id: i for i, n in enumerate(scope.traverse())}
    at = order.get(headline.mem_id, 0)
    nodes = [n for n in scope.css("time[datetime]") if not _in_teaser(n)]
    for node in sorted(nodes, key=lambda n: abs(order.get(n.mem_id, 0) - at)):
        yield node.attributes.get("datetime") or ""


def _date_candidates(tree: HTMLParser, ld: list[dict], headline=None) -> Iterator[str]:
    """Raw publication-date strings, most reliable first. <time> elements count only in the
    headline's block (see _headline_scope), closest first: news cards in sidebars are
    <article>s with <time>s too, and The Daily Star's <article> also holds its latest-news list."""
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
    scope = _headline_scope(headline)
    if scope is not None:
        yield from _times_near(headline, scope)
    else:
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


def _day_month_year(text: str, numeric: bool) -> tuple[int, int, int] | None:
    """(day, month, year) of the first date written out in normalised text. Bare numeric
    dates ("22-11-2026", day first) only when `numeric`."""
    if m := _DAY_MONTH_YEAR.search(text):
        return int(m[1]), _MONTHS[m[2].lower()], int(m[3])
    if m := _MONTH_DAY_YEAR.search(text):
        return int(m[2]), _MONTHS[m[1].lower()], int(m[3])
    if numeric and (m := _NUMERIC_DATE.search(text)):
        return int(m[1]), int(m[2]), int(m[3])
    return None


def _written_dt(value: str) -> datetime | None:
    """A date field written out ("১৭:২৫, ২২ নভেম্বর ২০২৬", "22-11-2026 03:15:50 পূর্বাহ্ন"),
    Dhaka time, with its clock time when it has one."""
    text = normalize_unicode(value)
    dmy = _day_month_year(text, numeric=True)
    if dmy is None:
        return None
    hour = minute = second = 0
    if m := _CLOCK.search(text):
        hour, minute, second = int(m[1]), int(m[2]), int(m[3] or 0)
        mark = (m[4] or "").lower()
        if mark in _PM and hour < 12:
            hour += 12
        elif mark == "রাত" and 6 <= hour < 12:  # রাত ৯টা = 21:00; রাত ২টা = 02:00
            hour += 12
        elif mark in _AM and hour == 12:
            hour = 0
    try:
        return datetime(dmy[2], dmy[1], dmy[0], hour, minute, second, tzinfo=_DHAKA)
    except ValueError:
        return None


_LIVE_CLOCK_MINUTES = 30


def _is_live_clock(text: str, day: datetime, now: datetime) -> bool:
    """A header clock: today's date with a time within _LIVE_CLOCK_MINUTES of now
    ("November 23, 2026, 9:21 pm" read at 21:21). Dropping it costs nothing: an undated article
    gets the scrape date anyway."""
    local = now.astimezone(_DHAKA)
    if day.date() != local.date() or not (m := _CLOCK.search(normalize_unicode(text))):
        return False
    hour, minute = int(m[1]), int(m[2])
    mark = (m[4] or "").lower()
    if mark in _PM and hour < 12:
        hour += 12
    elif mark in _AM and hour == 12:
        hour = 0
    shown = local.replace(hour=hour % 24, minute=minute % 60, second=0, microsecond=0)
    return abs((shown - local).total_seconds()) <= _LIVE_CLOCK_MINUTES * 60


def _text_to_day(text: str, now: datetime, last_resort: bool = False) -> datetime | None:
    """The first date written out in text, as midnight (Dhaka) of that day. A bare numeric
    date counts only after a publish label, or (last resort) an "updated" label."""
    text = normalize_unicode(text)
    day = month = year = None
    if m := _DAY_MONTH_YEAR.search(text):
        day, month, year = int(m[1]), _MONTHS[m[2].lower()], int(m[3])
    elif m := _MONTH_DAY_YEAR.search(text):
        day, month, year = int(m[2]), _MONTHS[m[1].lower()], int(m[3])
    elif (m := _LABELED_NUMERIC.search(text)) or (last_resort and (m := _UPDATED_NUMERIC.search(text))):
        day, month, year = (int(m[1]), int(m[2]), int(m[3])) if m[1] else (int(m[6]), int(m[5]), int(m[4]))
    elif m := _NUMERIC_THEN_LABEL.search(text):
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
    own = " ".join((node.text(deep=False) or "").split())  # templates pad meta lines with indentation
    if not own or len(own) > _MAX_META_CHARS:
        return None
    own_n = normalize_unicode(own)
    labeled = bool(_LABELED.search(own_n) or (last_resort and _UPDATED.search(own_n)))
    if labeled or _meta_like(node, last_resort=last_resort):
        if (dt := _text_to_day(own, now, last_resort)) is not None:
            return None if _is_live_clock(own, dt, now) else dt
        # "আপলোড সময় : <span>২২-১১-২০২৬ ...</span>": the label here, the date in a child.
        # ... or the label alone in a <b>/<strong>/<span>, the date beside it in the parent line.
        if labeled:
            for scope in (node, node.parent):
                whole = " ".join(scope.text().split()) if scope is not None else ""
                if whole and len(whole) <= _MAX_META_CHARS and (dt := _text_to_day(whole, now, last_resort)):
                    return None if _is_live_clock(whole, dt, now) else dt
    return None


_HEADLINE_TAGS = ("h2", "h3", "h4", "h5", "h6", "div", "p", "span", "strong")
_TITLE_PREFIX_KEY = 15  # key characters a headline must share with the page title
_MIN_TITLE_KEY = 8      # shorter titles are not matched (too likely to match something else)
_TITLE_SITE_SEP = re.compile(r"\s+(?:\||–|—|-|»|::)\s+")


def _title_keys(tree: HTMLParser, site_key: str) -> list[str]:
    """Key prefixes of the page's og:title and <title>, long enough to match safely and not
    the site name."""
    titles = []
    og = tree.css_first('meta[property="og:title"]')
    if og is not None:
        titles.append(og.attributes.get("content") or "")
    if (node := tree.css_first("title")) is not None:
        titles.append(node.text())
    keys = []
    for title in titles:
        # "Headline | Site name": a short headline's key would run into the site name.
        for part in (title, _TITLE_SITE_SEP.split(title)[0]):
            keys.append(_key(part)[:_TITLE_PREFIX_KEY])
    return [k for k in dict.fromkeys(keys)
            if len(k) >= _MIN_TITLE_KEY and not (site_key and site_key.startswith(k))]


def _inside(node, tags: tuple[str, ...]) -> bool:
    up = node.parent
    while up is not None:
        if up.tag in tags:
            return True
        up = up.parent
    return False


def _headline(tree: HTMLParser):
    """The article's headline element.

    The <h1> or heading-like element (outside links and menus) whose text starts like the page's
    og:title / <title>; pages also carry <h1>s for "Share", comments or the author's name, and
    some themes have no <h1> (h2.post-title, h4.detail-headline, div.single-page-title). With
    no title match, the longest real <h1>.
    """
    site_key = _site_key(tree)
    h1s = [h for h in tree.css("h1") if len(h.text().split()) >= _MIN_H1_WORDS and _key(h.text()) != site_key]
    keys = _title_keys(tree, site_key)
    for key in keys:
        for h in h1s:
            if _key(h.text()).startswith(key):
                return h
    # Outside links first; then inside one (comillarkagoj wraps its headline in a link to the
    # page itself). Never in a menu or list item: those are other stories' titles.
    for allowed_in_link in (False, True):
        for key in keys:
            for node in (tree.body or tree.root).traverse():
                if node.tag not in _HEADLINE_TAGS or len(node.text()) > _MAX_TITLE_CHARS:
                    continue
                if not _key(node.text()).startswith(key):
                    continue
                if allowed_in_link:  # <a><h3>..</h3></a> or <h3><a>..</a></h3>: a link to the page itself
                    if not _inside(node, ("li", "nav")):
                        return node
                elif node.css_first("a") is None and not _inside(node, ("a", "li", "nav")):
                    return node
    if h1s:
        return max(h1s, key=lambda h: len(" ".join(h.text().split())))
    return None


def _text_date(tree: HTMLParser, now: datetime, headline=False) -> datetime | None:
    """A visible date in the article's meta line, for pages without date markup.

    Only next to the headline: in the <h1>'s own header block, or in the text right after it.
    Dates earlier on the page are the masthead's "today" or other stories.
    """
    if headline is False:
        headline = _headline(tree)
    if headline is None:
        return None
    # 1. a meta line or labelled date; 2. a line that is only a date, right by the headline;
    # 3. an "updated" stamp, only when nothing else is near the headline: it is never earlier
    # than the publish date, so at worst an old story edited later looks newer.
    if (dt := _near_headline(tree, headline, now, last_resort=False)) is not None:
        return dt
    if (dt := _labeled_wide(headline, now)) is not None:
        return dt
    if (dt := _bare_line_near(tree, headline, now)) is not None:
        return dt
    return _near_headline(tree, headline, now, last_resort=True)


_LABELED_LEVELS = 6


def _labeled_wide(headline, now: datetime) -> datetime | None:
    """A labelled publish line ("প্রকাশ : ১৪ নভেম্বর, ২০২৬") up to _LABELED_LEVELS above the
    headline, outside teaser cards, the closest to the headline first. Themes put it in a
    reporter column beside the headline (karatoa, noakhalirkatha); related-story lists can
    carry labelled dates too, hence closest first."""
    scope = headline
    for _ in range(_LABELED_LEVELS):
        if scope.parent is None or scope.parent.tag in ("body", "html"):
            break
        scope = scope.parent
    order = {n.mem_id: i for i, n in enumerate(scope.traverse())}
    at = order.get(headline.mem_id, 0)
    found = []
    for node in scope.traverse():
        if node.tag in ("script", "style", "-text"):
            continue
        own = normalize_unicode(" ".join((node.text(deep=False) or "").split()))
        if not own or len(own) > _MAX_META_CHARS or not _LABELED.search(own) or _in_teaser(node):
            continue
        if (dt := _date_in(node, now)) is not None:
            found.append((abs(order.get(node.mem_id, 0) - at), dt))
    return min(found, key=lambda f: f[0])[1] if found else None


# A line that is nothing but a date: "শনিবার, ১৪ নভেম্বর ২০২৬", "ডিসেম্বর ১৩, ২০২৫",
# "২০ November ২০২৬ Friday ৪:৪৬:৪৮ PM". What may remain once the date is taken out: weekday,
# clock, am/pm words, "ইং"/"খ্রিস্টাব্দ", punctuation. A masthead line with the Bangla or Hijri
# date (৯ অগ্রহায়ণ ১৪৩৩ বঙ্গাব্দ) or a sentence leaves words behind, so it is not one.
_WEEKDAYS = (r"(?:শনি|রবি|রোব|সোম|মঙ্গল|বুধ|বৃহস্পতি|শুক্র)বার|saturday|sunday|monday|tuesday|wednesday"
             r"|thursday|friday|sat|sun|mon|tue|wed|thu|fri")
_LINE_FILLER = re.compile(rf"\b(?:{_WEEKDAYS})\b|(?:{_WEEKDAYS})|ইং|ইংরেজি|খ্রিস্টাব্দ|খ্রিঃ|খ্রি\.?|সময়|সময়|তারিখ"
                          r"|am|pm|a\.m\.|p\.m\.|এএম|পিএম|পূর্বাহ্ন|অপরাহ্ন|সকাল|ভোর|দুপুর|বিকাল|বিকেল|সন্ধ্যা|রাত|"
                          r"at|on|[\s,.:;|/\-–—•●◦·()\[\]॥]+", re.I)
_MAX_BARE_LINE = 70
_BARE_AFTER_CHARS = 300  # how far after the headline a bare date line may sit


_BYLINE_SEP = re.compile(r"\s*[/|•●]\s*")


def _bare_date_line(text: str) -> bool:
    text = normalize_unicode(text)
    # A byline "চাঁপাইনবাবগঞ্জ নিউজ / নিজস্ব প্রতিবেদক / ২২ নভেম্বর ২০২৬": its last part.
    parts = _BYLINE_SEP.split(text)
    if len(parts) > 1 and len(text) <= 2 * _MAX_BARE_LINE:
        return _bare_date_line(parts[-1]) or _bare_date_line(parts[0])
    if len(text) > _MAX_BARE_LINE:
        return False
    m = _DAY_MONTH_YEAR.search(text) or _MONTH_DAY_YEAR.search(text) or _NUMERIC_DATE.search(text)
    if not m:
        return False
    rest = text[:m.start()] + " " + text[m.end():]
    rest = _CLOCK.sub(" ", rest)
    return len(_LINE_FILLER.sub("", rest)) == 0


def _bare_line_near(tree: HTMLParser, headline, now: datetime) -> datetime | None:
    """A line that is only a date in the headline's block (2 levels up) or within
    _BARE_AFTER_CHARS after it. Today's date above the headline is taken for the masthead and
    skipped (an undated article gets the scrape date anyway)."""
    today = now.astimezone(_DHAKA).date()

    def date_of(node, before: bool) -> datetime | None:
        if node.tag in ("script", "style", "-text"):
            return None
        own = " ".join((node.text(deep=False) or "").split())
        if not own or not _bare_date_line(own):
            return None
        parts = _BYLINE_SEP.split(normalize_unicode(own))
        segment = next((s for s in (parts[-1], parts[0]) if _bare_date_line(s)), parts[-1]) if len(parts) > 1 else own
        dmy = _day_month_year(normalize_unicode(segment), numeric=True)  # a bare line may be "২২-১১-২০২৬"
        try:
            dt = datetime(dmy[2], dmy[1], dmy[0], tzinfo=_DHAKA) if dmy else None
        except ValueError:
            return None
        if (dt is None or dt.year < 2000 or dt > now + timedelta(days=1) or _is_live_clock(own, dt, now)
                or (before and dt.date() == today)):
            return None
        return dt

    block, level = headline.parent, 0
    while block is not None and block.tag not in ("body", "html") and level < 2:
        before = True
        for node in block.traverse():
            if node.mem_id == headline.mem_id:
                before = False
                continue
            if (dt := date_of(node, before)) is not None:
                return dt
        block, level = block.parent, level + 1
    after, chars = False, 0
    for node in (tree.body or tree.root).traverse():
        if node.mem_id == headline.mem_id:
            after = True
            continue
        if not after or node.tag in ("script", "style"):
            continue
        if (dt := date_of(node, before=False)) is not None:
            return dt
        own = node.text(deep=False)
        chars += len(own.strip()) if own else 0
        if chars > _BARE_AFTER_CHARS:
            break
    return None


def _near_headline(tree: HTMLParser, headline, now: datetime, last_resort: bool) -> datetime | None:
    # The headline's own block, up to 3 levels: some themes put the date line in a reporter box
    # in the next column (sylhettoday24).
    block, level = headline.parent, 0
    while block is not None and block.tag not in ("body", "html") and level < 3:
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


# A date in the article's URL: /2026/11/11/ or /2026-11-11 (a day), /2026/11/ (a month).
_URL_DAY = re.compile(r"/(20\d\d)[/-](\d\d)[/-](\d\d)(?=[/._-]|$)")
_URL_MONTH = re.compile(r"/(20\d\d)/(\d\d)(?=/)")


def _url_day(url: str | None) -> datetime | None:
    if url and (m := _URL_DAY.search(urlsplit(url).path)):
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=_DHAKA)
        except ValueError:
            return None
    return None


def _url_month(url: str | None) -> tuple[int, int] | None:
    if url and (m := _URL_MONTH.search(urlsplit(url).path)) and 1 <= int(m[2]) <= 12:
        return int(m[1]), int(m[2])
    return None


def _published(tree: HTMLParser, ld: list[dict], fallback: str | None,
               now: datetime | None = None, url: str | None = None) -> tuple[datetime | None, bool]:
    """(published_at, date_only). A full timestamp beats a bare day from a better source,
    because a bare day read as midnight can push yesterday's story out of the window."""
    day: datetime | None = None
    headline = _headline(tree)
    for raw in _date_candidates(tree, ld, headline):
        dt = _parse_dt(raw)
        if dt is None:
            continue
        if not _bare_day(raw, dt):
            return dt, False
        day = day or dt
    if day is not None:
        return day, True
    # Last resorts: a date written in the article's meta line; the first <time> anywhere, only
    # when no headline was found (otherwise it is a sidebar story's: The Daily Star's first
    # <time> is "16 minutes ago" in the latest-news list); then trafilatura's guess (which can
    # pick up the header's "today" date).
    now = now or datetime.now(_DHAKA)
    if (dt := _text_date(tree, now, headline)) is not None:
        return dt, True
    # A <time> elsewhere in the story's <article> (some themes put it at the end), closest first.
    if (article := _headline_scope(headline, article=True)) is not None:
        for raw in _times_near(headline, article):
            if (dt := _parse_dt(raw)) is not None:
                return dt, _bare_day(raw, dt)
    if headline is None:
        time_node = tree.css_first("time[datetime]")
        raw = time_node.attributes.get("datetime") if time_node is not None else None
        if (dt := _parse_dt(raw)) is not None:
            return dt, _bare_day(raw, dt)
    # The day in the URL (/2026/11/11/), before trafilatura's guess.
    if (dt := _url_day(url)) is not None and dt <= now + timedelta(days=1):
        return dt, True
    if (dt := _parse_dt(fallback)) is not None:
        bare = _bare_day(fallback, dt)
        # A bare "today" from the guess is almost always the header clock, which would make every
        # old story look new. No date is safer: a saved article falls back to the scrape date.
        if bare and dt.astimezone(_DHAKA).date() == now.astimezone(_DHAKA).date():
            return None, False
        # A guess outside the URL's month (/2026/11/blog-post.html guessed as 2023-02-24) is wrong.
        local = dt.astimezone(_DHAKA)
        if (month := _url_month(url)) is not None and (local.year, local.month) != month:
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
    published, date_only = _published(tree, ld, doc.get("date"), now, url)

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
