"""Regional tabs without listing dates: paging is judged by the dates of the last articles."""

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from avn_scraper.scraper.config import Source
from avn_scraper.scraper.discover.sections import discover_sections
from avn_scraper.scraper.fetch import Fetcher

SITE = "http://93.184.215.14"
NOW = datetime.now(timezone.utc)
FRESH = (NOW - timedelta(hours=2)).isoformat()
OLD = (NOW - timedelta(days=40)).isoformat()


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


def _article(date: str) -> str:
    return (f'<html><head><meta property="article:published_time" content="{date}"></head>'
            f'<body><article><h1>শিরোনাম এখানে</h1><p>{"খবর। " * 40}</p></article></body></html>')


def _listing(ids: list[int], nxt: str | None) -> str:
    links = "".join(f'<a href="/news/{i}">স্থানীয় খবরের শিরোনাম নম্বর {i}</a>' for i in ids)
    more = f'<a rel="next" href="{nxt}">পরের পাতা</a>' if nxt else ""
    widget = '<aside><a href="/news/99">সবচেয়ে পঠিত খবরের শিরোনাম</a></aside>'
    return f"<html><body><div>{links}</div>{widget}{more}</body></html>"


HOME = ('<html><body><nav class="menu"><ul><li><a href="/local">স্থানীয়</a></li></ul></nav>'
        '<a href="/news/99">সবচেয়ে পঠিত খবরের শিরোনাম</a></body></html>')


def _run(dates: dict[int, str], kind: str = "regional"):
    pages = {"/local": _listing([1, 2, 3, 4, 5], "/local?page=2"), "/local?page=2": _listing([6, 7], None)}
    pages |= {f"/news/{i}": _article(d) for i, d in dates.items()}
    pages["/news/99"] = _article(OLD)  # an old "most read" story in a widget on every page
    requests: Counter = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.raw_path.decode()
        requests[key] += 1
        html = pages.get(key)
        if html is None:
            return httpx.Response(404, stream=_Stream(b"no"))
        return httpx.Response(200, stream=_Stream(html.encode()), headers={"Content-Type": "text/html"})

    source = Source(id="local", name="Local", language="bn", type=kind, homepage=SITE + "/")

    async def go():
        async with Fetcher({"respect_robots": False, "per_host_delay": 0, "retries": 0},
                           transport=httpx.MockTransport(handler)) as f:
            return await discover_sections(f, source, HOME, NOW - timedelta(hours=24), {}, None)

    items, _, pages_read = asyncio.run(go())
    return items, pages_read, requests


def test_old_bottom_of_page_stops_paging():
    items, pages_read, requests = _run({1: FRESH, 2: FRESH, 3: OLD, 4: OLD, 5: OLD})
    assert pages_read == 1 and requests["/local?page=2"] == 0
    # the probed old articles carry their date (filtered before download), the pages are kept
    probed = {c.url.rsplit("/", 1)[-1]: c for c in items if c.prefetched}
    assert set(probed) == {"3", "4", "5"} and all(c.published_at for c in probed.values())
    assert requests["/news/99"] == 0  # the homepage widget is never probed


def test_old_stories_pinned_on_top_do_not_stop_paging():
    items, pages_read, requests = _run({1: OLD, 2: OLD, 3: FRESH, 4: FRESH, 5: FRESH, 6: OLD, 7: OLD})
    assert pages_read == 2 and requests["/local?page=2"] == 1
    fresh = [c for c in items if c.prefetched and c.url.endswith(("/3", "/4", "/5"))]
    assert fresh and all(c.published_at is None for c in fresh)  # left to the pipeline's date check


def test_national_outlets_are_not_probed():
    _, pages_read, requests = _run({i: OLD for i in range(1, 8)}, kind="national")
    assert pages_read == 2 and not any(k.startswith("/news/") for k in requests)


def test_pipeline_reuses_a_probed_page():
    from avn_scraper.scraper.discover import Candidate
    from avn_scraper.scraper.pipeline import _fetch_one, _merge

    first = Candidate(url=SITE + "/news/1", source_id="local", via="listing")
    probed = Candidate(url=SITE + "/news/1", source_id="local", via="listing", prefetched=("<html>", "EX"))
    merged = _merge([first, probed])
    c = next(iter(merged.values()))

    def handler(request):
        raise AssertionError("downloaded again")

    async def go():
        async with Fetcher({"respect_robots": False, "per_host_delay": 0, "retries": 0},
                           transport=httpx.MockTransport(handler)) as f:
            return await _fetch_one(f, c)

    assert asyncio.run(go()) == (c, "<html>", "EX")


def _budget_run(kind: str, budget_minutes: float):
    """discover_source on an outlet with one two-page tab; returns (pages read, counts)."""
    from avn_scraper.scraper.discover.sources import discover_source

    pages = {"/": HOME, "/local": _listing([1, 2, 3], "/local?page=2"), "/local?page=2": _listing([4, 5], None)}

    def handler(request: httpx.Request) -> httpx.Response:
        html = pages.get(request.url.raw_path.decode(), "")
        return httpx.Response(200, stream=_Stream(html.encode()), headers={"Content-Type": "text/html"})

    source = Source(id="local", name="Local", language="bn", type=kind, homepage=SITE + "/",
                    feeds=[SITE + "/feed"])
    cache = type("Cache", (), {"get": lambda self, k: None, "set": lambda self, *a: None})()

    async def go():
        async with Fetcher({"respect_robots": False, "per_host_delay": 0, "retries": 0},
                           transport=httpx.MockTransport(handler)) as f:
            return await discover_source(f, source, NOW - timedelta(hours=24), cache,
                                         {"regional_budget_minutes": budget_minutes, "date_probe": 0})
    r = asyncio.run(go())
    return r.counts["section_pages"], r.counts


def test_regional_budget_stops_tab_paging_and_keeps_what_was_found():
    pages_read, counts = _budget_run("regional", -1)  # negative: no budget
    assert pages_read == 2 and "section_budget_hit" not in counts
    pages_read, counts = _budget_run("regional", 1e-9)  # used up at once
    assert pages_read == 0 and counts["section_budget_hit"] == 1
    assert counts["listing"] > 0 and "rss" in counts  # homepage and feeds still read


def test_national_papers_have_no_budget():
    pages_read, counts = _budget_run("national", 1e-9)
    assert pages_read == 2 and "section_budget_hit" not in counts


def test_regional_tabs_read_at_most_regional_max_pages():
    pages = {f"/local?page={n}": _listing(list(range(n * 10, n * 10 + 5)), f"/local?page={n + 1}")
             for n in range(2, 10)}
    pages["/local"] = _listing([1, 2, 3, 4, 5], "/local?page=2")
    requests: Counter = Counter()

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.raw_path.decode()
        requests[key] += 1
        return httpx.Response(200, stream=_Stream(pages.get(key, "").encode()), headers={"Content-Type": "text/html"})

    async def go(kind):
        source = Source(id="local", name="Local", language="bn", type=kind, homepage=SITE + "/")
        async with Fetcher({"respect_robots": False, "per_host_delay": 0, "retries": 0},
                           transport=httpx.MockTransport(handler)) as f:
            return await discover_sections(f, source, HOME, NOW - timedelta(hours=24),
                                           {"date_probe": 0, "regional_max_pages": 3}, None)

    _, _, read = asyncio.run(go("regional"))
    assert read == 3
    _, _, read = asyncio.run(go("national"))
    assert read > 3
