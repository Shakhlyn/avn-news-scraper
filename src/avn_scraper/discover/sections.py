"""Section tabs (national, international, সারাদেশ, ...) found in the site menu, with pagination."""

from __future__ import annotations

import re
from datetime import datetime
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
_NOT_SECTION = re.compile(
    r"/(login|signin|register|signup|search|videos?|photos?|gallery|galleries|e-?paper|archives?|about|contact"
    r"|privacy|terms|advertis\w*|subscri\w*|rss|feeds?|sitemap|authors?|podcasts?|tags?|careers?|jobs?|page"
    r"|cdn-cgi|converter|live|(opinion-)?polls?|horoscope|quiz)"
    r"(?=[/_.-]|$)", re.I)
_FILE_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|webp|xml|zip|mp4|apk)$", re.I)
_ARTICLE_ID = re.compile(r"\d{5,}")
_MAX_DEPTH = 3
_MAX_SLUG_CHARS = 40
_MAX_TEXT_CHARS = 30
_MAX_TEXT_WORDS = 4

_PAGE_KEYS = ("page", "pg", "p", "paged", "pageno", "page_no")
_PATH_PAGE = re.compile(r"/page/(\d+)$")


def _in_menu(node) -> bool:
    node = node.parent
    while node is not None:
        if node.tag in ("nav", "header", "footer"):
            return True
        attrs = node.attributes
        if attrs.get("role") == "navigation" or _MENU_ATTR.search(f"{attrs.get('class') or ''} {attrs.get('id') or ''}"):
            return True
        node = node.parent
    return False


def _looks_like_section(url: str, text: str, homepage: str) -> bool:
    if not url.startswith("http") or host_of(url) != host_of(homepage):
        return False
    path = unquote(urlsplit(url).path).rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments or len(segments) > _MAX_DEPTH:
        return False
    last = segments[-1]
    if len(last) > _MAX_SLUG_CHARS or _ARTICLE_ID.search(last):
        return False
    if _NOT_SECTION.search(path.lower()) or _FILE_EXT.search(path):
        return False
    return 2 <= len(text) <= _MAX_TEXT_CHARS and len(text.split()) <= _MAX_TEXT_WORDS


def find_section_links(html: str, page_url: str, homepage: str, limit: int) -> list[str]:
    """Links in the page's menus (nav/header/footer) that look like section tabs.

    Top-level tabs come first (in menu order), then sub-tabs, so `limit` drops sub-tabs first.
    """
    tree = HTMLParser(html)
    seen = {canonicalize(homepage)}
    out = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        text = " ".join(a.text(separator=" ").split())
        if not _looks_like_section(url, text, homepage) or not _in_menu(a):
            continue
        key = canonicalize(url)
        if key not in seen:
            seen.add(key)
            out.append(url)
    return sorted(out, key=lambda u: len([s for s in urlsplit(u).path.split("/") if s]))[:limit]


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


def page_is_past_window(html: str, cutoff: datetime) -> bool:
    """True when most dated items (<time datetime>) on the page are older than cutoff."""
    dates = [d for node in HTMLParser(html).css("time[datetime]") if (d := parse_dt(node.attributes.get("datetime")))]
    return bool(dates) and sum(d < cutoff for d in dates) * 2 > len(dates)


async def discover_sections(fetcher: Fetcher, source: Source, homepage_html: str, cutoff: datetime,
                            settings: dict | None, memory: ListingMemory | None
                            ) -> tuple[list[Candidate], list[str], int]:
    """Scan every section tab and follow its pages. Returns (candidates, sections, pages scanned).

    Paging stops at max_pages, when a page brings nothing new, when at least half its links
    were already seen on an earlier run, or when its dated items are older than cutoff.
    """
    settings = settings or {}
    limit = int(settings.get("max_per_source", 60))
    max_pages = int(settings.get("max_pages", 5))
    candidates = list(source.sections)
    if settings.get("auto_discover", True) and source.auto_sections and homepage_html:
        candidates += find_section_links(homepage_html, source.homepage, source.homepage, limit)
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
            if old * 2 >= len(ids) or page_is_past_window(html, cutoff):
                break
            url = find_next_page(html, url)
    return out, sections, pages
