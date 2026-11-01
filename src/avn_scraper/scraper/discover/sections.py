"""Section tabs (national, international, latest, ...) and the print edition, with pagination."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import Source
from ..fetch import Fetcher
from ..urls import article_id, canonicalize, host_of
from . import Candidate
from .listing import discover_listing
from .sitemap import parse_dt

if TYPE_CHECKING:
    from .sources import ListingMemory

_MENU_ATTR = re.compile(r"menu|nav", re.I)
_SUBMENU_ATTR = re.compile(r"sub-?menu|sub-?nav|dropdown|mega|children", re.I)
_DROPDOWN = "ul, ol, [class*=dropdown-menu], [class*=sub-menu], [class*=submenu], [class*=mega]"
_NOT_SECTION = re.compile(
    r"/(login|signin|register|signup|search|videos?|photos?|gallery|galleries|e-?paper|archives?|about|contact"
    r"|privacy|terms|advertis\w*|subscri\w*|rss|feeds?|sitemap|authors?|podcasts?|tags?|topics?|careers?|jobs?|page"
    r"|cdn-cgi|[\w-]*converter|live|(opinion-)?polls?|horoscope|quiz|comment-policy|[\w-]*rate-card|apps?|user"
    r"|home|press-release|photo\w*|media\w*|tube|magazine-archive)"
    r"(?=[/_.-]|$)", re.I)
# The print edition / e-paper as an HTML list of articles on the outlet's own site.
_PRINT_PATH = re.compile(r"print|e-?paper|todays?-?paper|ajker-?patrika|ছাপা", re.I)
_PRINT_TEXT = re.compile(r"আজকের পত্রিকা|ছাপা সংস্করণ|প্রিন্ট সংস্করণ|প্রিন্ট ভার্সন|print edition|print version|today'?s paper", re.I)
_FILE_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|xml|zip|mp4|apk)$", re.I)
_ARTICLE_ID = re.compile(r"\d{5,}")
_URL_DATE = re.compile(r"/(20\d\d)[/-](\d\d)[/-](\d\d)(?=[/-]|$)")
_MAX_DEPTH = 3
_MAX_SLUG_CHARS = 40
_MAX_TEXT_CHARS = 30
_MAX_TEXT_WORDS = 4
_MAX_PRINT_LINKS = 3

_PAGE_KEYS = ("page", "pg", "p", "paged", "pageno", "page_no")
_PATH_PAGE = re.compile(r"/page/(\d+)$")


def _menu_level(node, page_url: str, homepage: str) -> str | None:
    """Where a link sits in the site menu: "parent" (a top-level item that opens a dropdown),
    "top", "sub" (in a dropdown under another tab), or None (not in a menu, or in a dropdown
    such as "More" / a hamburger list whose button is not itself a tab)."""
    in_menu, sub, items = False, False, []
    node = node.parent
    while node is not None:
        if node.tag == "footer":
            return None
        attrs = node.attributes
        names = f"{attrs.get('class') or ''} {attrs.get('id') or ''}"
        if node.tag == "li":
            items.append(node)
        elif _SUBMENU_ATTR.search(names):
            sub = True
        if node.tag in ("nav", "header") or attrs.get("role") == "navigation" or _MENU_ATTR.search(names):
            in_menu = True
        node = node.parent
    if not in_menu:
        return None
    if sub or len(items) > 1:
        owner = items[1] if len(items) > 1 else None  # the menu item the dropdown opens from
        button = owner.css_first("a[href]") if owner is not None else None
        href = (button.attributes.get("href") or "").strip() if button is not None else ""
        tab = href and not href.startswith(("#", "javascript:")) and _section_path(urljoin(page_url, href), homepage)
        return "sub" if tab else None
    return "parent" if items and items[0].css_first(_DROPDOWN) is not None else "top"


def _section_path(url: str, homepage: str) -> str | None:
    """Unquoted path of a same-host, non-article URL at most _MAX_DEPTH deep, else None."""
    if not url.startswith("http") or host_of(url) != host_of(homepage):
        return None
    path = unquote(urlsplit(url).path).rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments or len(segments) > _MAX_DEPTH:
        return None
    last = segments[-1]
    if len(last) > _MAX_SLUG_CHARS or _ARTICLE_ID.search(last) or _FILE_EXT.search(last):
        return None
    return path


def _links(html: str, page_url: str):
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            yield a, urljoin(page_url, href).split("#")[0], " ".join(a.text(separator=" ").split())


def find_section_links(html: str, page_url: str, homepage: str, limit: int) -> list[str]:
    """Top-level tabs of the site's main menu (national, international, latest, ...), in menu order.

    A link that also sits in a dropdown anywhere (sites often repeat menus flat for mobile)
    counts as a sub-tab and is left out, unless it is the item that opens a dropdown.
    """
    top: dict[str, str] = {}
    parents: set[str] = set()
    sub = {canonicalize(homepage)}
    for a, url, text in _links(html, page_url):
        path = _section_path(url, homepage)
        if (path is None or _NOT_SECTION.search(path.lower()) or not 2 <= len(text) <= _MAX_TEXT_CHARS
                or len(text.split()) > _MAX_TEXT_WORDS):
            continue
        level = _menu_level(a, page_url, homepage)
        if level == "sub":
            sub.add(canonicalize(url))
        elif level is not None:
            top.setdefault(canonicalize(url), url)
            if level == "parent":
                parents.add(canonicalize(url))
    return [url for key, url in top.items() if key in parents or key not in sub][:limit]


def find_print_links(html: str, page_url: str, homepage: str) -> list[str]:
    """Links (anywhere on the page) to the print edition / e-paper listed on the outlet's own site."""
    seen = {canonicalize(homepage)}
    out = []
    for _, url, text in _links(html, page_url):
        path = _section_path(url, homepage)
        if path is None or not (_PRINT_PATH.search(path) or _PRINT_TEXT.search(text)):
            continue
        if _NOT_SECTION.search(path.lower()) and not _PRINT_PATH.search(path):
            continue  # e.g. Ajker Patrika's "about us", named after the paper (আজকের পত্রিকা)
        key = canonicalize(url)
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out[:_MAX_PRINT_LINKS]


def _page_key(url: str) -> tuple[str, frozenset]:
    parts = urlsplit(url)
    return parts.path.rstrip("/") or "/", frozenset(parse_qsl(parts.query))


def _next_page_keys(page_url: str) -> list[tuple[str, frozenset]]:
    """What the next page's URL would be under common pagination schemes, most likely first."""
    parts = urlsplit(page_url)
    path = parts.path.rstrip("/")
    query = dict(parse_qsl(parts.query))
    current = next((k for k in _PAGE_KEYS if query.get(k, "").isdigit()), None)
    if current:
        return [(path or "/", frozenset({**query, current: str(int(query[current]) + 1)}.items()))]
    m = _PATH_PAGE.search(path)
    if m:
        return [(f"{path[:m.start()]}/page/{int(m[1]) + 1}", frozenset(query.items()))]
    # First page: WordPress /page/2, then ?page=2 (1-based), then ?page=1 (0-based, e.g. Drupal).
    keys = [(f"{path}/page/2", frozenset(query.items()))]
    keys += [(path or "/", frozenset({**query, k: n}.items())) for n in ("2", "1") for k in _PAGE_KEYS]
    return keys


def find_next_page(html: str, page_url: str) -> str | None:
    """The "next page" link of a listing page: rel=next, or the same URL with the page number + 1."""
    tree = HTMLParser(html)
    host = host_of(page_url)
    for node in tree.css("link[rel~=next], a[rel~=next]"):
        href = node.attributes.get("href")
        if href:
            url = urljoin(page_url, href)
            if url.startswith("http") and host_of(url) == host:
                return url
    links: dict[tuple[str, frozenset], str] = {}
    for a in tree.css("a[href]"):
        url = urljoin(page_url, a.attributes.get("href") or "").split("#")[0]
        if url.startswith("http") and host_of(url) == host:
            links.setdefault(_page_key(url), url)
    return next((links[k] for k in _next_page_keys(page_url) if k in links), None)


def _url_day_end(url: str) -> datetime | None:
    """End of the day in an article URL like /2026/10/21/... (day precision, so err late)."""
    m = _URL_DATE.search(url)
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc) + timedelta(days=1)
    except ValueError:
        return None


def page_is_past_window(html: str, cutoff: datetime, urls: list[str] = ()) -> bool:
    """True when most dated items on the page are older than cutoff.

    Dates come from <time datetime> elements, else from dates in the article URLs.
    """
    dates = [d for node in HTMLParser(html).css("time[datetime]") if (d := parse_dt(node.attributes.get("datetime")))]
    if not dates:
        dates = [d for u in urls if (d := _url_day_end(u))]
    return bool(dates) and sum(d < cutoff for d in dates) * 2 > len(dates)


async def discover_sections(fetcher: Fetcher, source: Source, homepage_html: str, cutoff: datetime,
                            settings: dict | None, memory: ListingMemory | None
                            ) -> tuple[list[Candidate], list[str], int]:
    """Scan the print edition and every top-level tab, following each one's pages.

    Returns (candidates, sections, pages scanned). Paging stops when most of a page's dated
    items are older than cutoff, when at least half its links were seen on an earlier run
    (the previous daily run), when a page brings nothing new, or at max_pages (a safety cap).
    """
    settings = settings or {}
    limit = int(settings.get("max_per_source", 60))
    max_pages = int(settings.get("max_pages", 20))
    candidates = list(source.sections)
    if settings.get("auto_discover", True) and source.auto_sections and homepage_html:
        candidates += await asyncio.to_thread(find_print_links, homepage_html, source.homepage, source.homepage)
        candidates += await asyncio.to_thread(find_section_links, homepage_html, source.homepage, source.homepage, limit)
    sections, keys = [], set()
    for u in candidates:
        if canonicalize(u) not in keys:
            keys.add(canonicalize(u))
            sections.append(u)
    sections = sections[:limit]

    out: list[Candidate] = []
    pages = 0
    for section in sections:
        found: set[str] = set()
        visited: set[str] = set()
        url: str | None = section
        while url and canonicalize(url) not in visited and len(visited) < max_pages:
            visited.add(canonicalize(url))
            items, html = await discover_listing(fetcher, url, source.homepage, source.id)
            pages += 1
            out += items
            ids = {article_id(c.url) for c in items}
            new = ids - found
            found |= ids
            if not html or not new:
                break
            old = memory.seen_before(source.id, ids) if memory is not None else 0
            if memory is not None:
                memory.record(source.id, ids)
            if old * 2 >= len(ids) or await asyncio.to_thread(page_is_past_window, html, cutoff, [c.url for c in items]):
                break
            url = await asyncio.to_thread(find_next_page, html, url)
    return out, sections, pages
