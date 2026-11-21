"""Old articles from an outlet's archive sitemaps, for backfilling past months.

Most outlets split their sitemap by date: one file per day (`sitemap-daily-2025-05-01.xml`),
per month (`2025-05-01.xml`, `archive_2025-05-01.xml`) or per year
(`sitemaps/2025/sitemap-2025-1.xml`). The index often lists only recent files, so the
URL pattern is learned from the files it does list and filled in for the dates asked
for. WordPress-style sitemaps (`post-sitemap12.xml`) carry no date in the name; their
files and entries are chosen by `lastmod`.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from lxml import etree

from ..config import Source
from ..fetch import Fetcher
from ..urls import same_site
from . import Candidate
from .sitemap import parse_sitemap

log = logging.getLogger(__name__)

_DAY = re.compile(r"(?<!\d)((?:19|20)\d\d)([-/_]?)(0[1-9]|1[0-2])\2(0[1-9]|[12]\d|3[01])(?!\d)")
_MONTH = re.compile(r"(?<!\d)((?:19|20)\d\d)([-/_])(0[1-9]|1[0-2])(?![-/_]?\d)")
_YEAR = re.compile(r"/((?:19|20)\d\d)/(?=[^/]*\1)")  # sitemaps/2025/sitemap-2025-1.xml
# Child sitemaps that never list articles.
_NOT_ARTICLES = re.compile(r"(image|video|tag|author|categor|static|section|topic|page|gallery|photo|"
                           r"taxonom|geo-|blog|writer|reporter)", re.I)
_LASTMOD_SLACK = timedelta(days=31)


def _template(url: str, match: re.Match, fmt: str) -> str:
    """The URL with the matched date replaced by strftime codes (other % escaped)."""
    return url[:match.start()].replace("%", "%%") + fmt + url[match.end():].replace("%", "%%")


@dataclass
class ArchiveLayout:
    """How one outlet's sitemaps are split by date."""
    source_id: str
    daily: set[str] = field(default_factory=set)    # strftime templates, one file per day
    monthly: set[str] = field(default_factory=set)  # one file per month
    yearly: set[str] = field(default_factory=set)   # templates with {year}
    undated: list[tuple[str, datetime | None]] = field(default_factory=list)  # (file, lastmod)
    entries: list[dict] = field(default_factory=list)  # articles listed in the index files themselves
    indexes: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        kinds = [k for k in ("daily", "monthly", "yearly") if getattr(self, k)]
        if not kinds and (self.undated or self.entries):
            kinds = ["lastmod"]
        return "+".join(kinds) or "none"


def learn_layout(source_id: str, children: list[tuple[str, datetime | None]],
                 entries: list[dict] | None = None) -> ArchiveLayout:
    """Sort an index's child sitemaps into dated templates and undated files."""
    layout = ArchiveLayout(source_id, entries=list(entries or []))
    by_template: dict[str, list[str]] = {}
    for loc, lastmod in children:
        path = loc.split("?", 1)[0]
        if _NOT_ARTICLES.search(path.rsplit("/", 1)[-1]):
            continue
        if m := _YEAR.search(loc):
            year = m[1]
            layout.yearly.add(loc.replace(year, "{year}"))
        elif m := _DAY.search(loc):
            sep = m[2]
            by_template.setdefault(_template(loc, m, f"%Y{sep}%m{sep}%d"), []).append(m[4])
        elif m := _MONTH.search(loc):
            layout.monthly.add(_template(loc, m, f"%Y{m[2]}%m"))
        else:
            layout.undated.append((loc, lastmod))
    for tmpl, days in by_template.items():
        # Files dated only on the 1st are one per month (ittefaq: 2025-05-01.xml).
        (layout.monthly if len(days) > 1 and set(days) == {"01"} else layout.daily).add(tmpl)
    return layout


def month_window(month: date, tz: ZoneInfo, today: date | None = None) -> tuple[datetime, datetime]:
    """[first moment, moment after the last day) of the month in tz, never past today."""
    last = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
    if today and last >= today:
        last = today - timedelta(days=1)
    return datetime.combine(month, time(), tz), datetime.combine(last + timedelta(days=1), time(), tz)


async def _read(fetcher: Fetcher, url: str) -> tuple[list[tuple[str, datetime | None]], list[dict]] | None:
    resp = await fetcher.get(url, "sitemap")
    if resp is None:
        return None
    try:
        return await asyncio.to_thread(parse_sitemap, resp.content)
    except (etree.XMLSyntaxError, OSError, ValueError, zlib.error):
        return None


async def discover_layout(fetcher: Fetcher, source: Source) -> ArchiveLayout:
    """Read the outlet's sitemap index(es) once and learn how they are split by date."""
    robots = [u for u in await fetcher.robots_sitemaps(source.homepage) if same_site(u, source.homepage)]
    candidates = list(dict.fromkeys(source.sitemaps + robots)) or [source.homepage.rstrip("/") + "/sitemap.xml"]
    # News sitemaps hold only the last two days.
    indexes = [u for u in candidates if "news" not in u.lower().rsplit("/", 1)[-1]] or candidates
    children: list[tuple[str, datetime | None]] = []
    entries: list[dict] = []
    read: list[str] = []
    for url in indexes[:3]:
        parsed = await _read(fetcher, url)
        if parsed is None:
            continue
        read.append(url)
        children += [c for c in parsed[0] if same_site(c[0], source.homepage)]
        entries += parsed[1]
    layout = learn_layout(source.id, list(dict.fromkeys(children)), entries)
    layout.indexes = read
    return layout


def _family(loc: str) -> str:
    """post-sitemap12.xml -> post-sitemap.xml: files of one numbered series."""
    return re.sub(r"\d+", "", loc.split("?", 1)[0].rsplit("/", 1)[-1])


def _newest_first(item: tuple[str, datetime | None]) -> tuple[str, int]:
    """WordPress numbers sitemap files oldest first (post-sitemap.xml is number 1)."""
    numbers = re.findall(r"\d+", item[0].split("?", 1)[0].rsplit("/", 1)[-1])
    return _family(item[0]), -int(numbers[-1]) if numbers else -1


@dataclass
class ArchiveResult:
    source_id: str
    candidates: list[Candidate] = field(default_factory=list)
    files: int = 0          # sitemap files read
    missing: int = 0        # dated files that did not exist (404) or failed
    error: str | None = None


class ArchiveReader:
    """Candidates for one month from one outlet. Undated and yearly files are kept in
    memory, so later months of the same backfill don't download them again."""

    def __init__(self, fetcher: Fetcher | None = None):
        self.fetcher = fetcher
        self._cache: dict[str, tuple[list, list] | None] = {}

    def use(self, fetcher: Fetcher) -> None:
        """Read through this fetcher from now on (each backfill month has its own)."""
        self.fetcher = fetcher

    async def _cached(self, url: str):
        if url not in self._cache:
            self._cache[url] = await _read(self.fetcher, url)
        return self._cache[url]

    async def month(self, source: Source, layout: ArchiveLayout, start: datetime, end: datetime) -> ArchiveResult:
        result = ArchiveResult(source.id)
        found: dict[str, Candidate] = {}

        def add(entries: list[dict], trusted: bool) -> None:
            """trusted: the file is named after this period, so its entries belong to it
            even when lastmod says they were edited later."""
            for e in entries:
                loc, when = e["loc"], e["published_at"]
                if not loc.startswith(("http://", "https://")) or not same_site(loc, source.homepage):
                    continue
                in_window = when is not None and start - timedelta(days=1) <= when < end + timedelta(days=1)
                if not trusted:
                    # lastmod is never before publication: older entries are out; much
                    # later ones were edited long after and are (mostly) not ours.
                    if when is None or when < start or when >= end + _LASTMOD_SLACK:
                        continue
                if loc not in found:
                    found[loc] = Candidate(url=loc, source_id=source.id, title=e["title"],
                                           published_at=when if in_window else None, via="archive")

        async def dated(url: str, trusted: bool) -> None:
            parsed = await _read(self.fetcher, url)
            if parsed is None:
                result.missing += 1
                return
            result.files += 1
            children, entries = parsed
            add(entries, trusted)
            for child, _ in children[:40]:  # a dated index (one file per section)
                if same_site(child, source.homepage) and (sub := await _read(self.fetcher, child)):
                    result.files += 1
                    add(sub[1], trusted)

        days = [start.date() + timedelta(days=i) for i in range((end.date() - start.date()).days)]
        jobs = [dated(d.strftime(t), True) for t in sorted(layout.daily) for d in days]
        jobs += [dated(start.strftime(t), True) for t in sorted(layout.monthly)]
        await asyncio.gather(*jobs)
        for tmpl in sorted(layout.yearly):
            parsed = await self._cached(tmpl.replace("{year}", str(start.year)))
            if parsed is None:
                result.missing += 1
                continue
            result.files += 1
            add(parsed[1], False)
        if not (layout.daily or layout.monthly or layout.yearly):
            add(layout.entries, False)
            older: set[str] = set()  # families already read back past the month
            for loc, lastmod in sorted(layout.undated, key=_newest_first):
                family = _family(loc)
                if family in older or (lastmod is not None and lastmod < start):
                    continue  # its newest entry is older than the month
                parsed = await self._cached(loc)
                if parsed is None:
                    continue
                result.files += 1
                add(parsed[1], False)
                dates = [e["published_at"] for e in parsed[1] if e["published_at"]]
                if dates and max(dates) < start:
                    older.add(family)  # numbered oldest first: the lower numbers are older still
        result.candidates = list(found.values())
        return result


def headline_text(c: Candidate) -> bool:
    """Is there a headline to keyword-filter on: a sitemap title, or a slug made of words
    (`.../3-banks-get-emergency-funds-3789026`, Bangla slugs too), not an id (`/uyrdgbm7hw`)?"""
    if c.title.strip():
        return True
    segments = [s for s in unquote(urlsplit(c.url).path).split("/") if s]
    if not segments:
        return False
    longest = max(segments, key=len)
    words = [w for w in re.split(r"[-_+\s]+", longest) if any(ch.isalpha() for ch in w)]
    return len(words) >= 2
