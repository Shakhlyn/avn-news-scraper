"""Backfill of past months from archive sitemaps (avn backfill)."""

from collections import Counter
from datetime import date, datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import pytest

from avn_scraper.scraper.backfill import BackfillOptions, RunGate, parse_months, run_backfill
from avn_scraper.scraper.config import Config, Source
from avn_scraper.scraper.discover import Candidate
from avn_scraper.scraper.discover.archive import headline_text, learn_layout, month_window
from avn_scraper.scraper.lock import is_locked, run_lock
from avn_scraper.scraper.storage import PostgresStorage

DHAKA = ZoneInfo("Asia/Dhaka")
SITE = "http://93.184.215.14"  # public IP literal: passes the SSRF guard without DNS
TODAY = date(2026, 11, 19)


# -- months ------------------------------------------------------------------

def test_parse_months_forms():
    assert parse_months(["2025-05"], TODAY) == [date(2025, 5, 1)]
    assert parse_months(["2025-07", "2025-05"], TODAY) == [date(2025, 5, 1), date(2025, 7, 1)]
    assert parse_months(["2025-11..2026-02"], TODAY) == [date(2025, 11, 1), date(2025, 12, 1),
                                                         date(2026, 1, 1), date(2026, 2, 1)]
    assert len(parse_months(["2025"], TODAY)) == 12
    assert parse_months(["2026"], TODAY)[-1] == date(2026, 11, 1)  # a year: only months so far
    assert parse_months(["2025-1,2025-03"], TODAY) == [date(2025, 1, 1), date(2025, 3, 1)]


@pytest.mark.parametrize("spec", ["2026-12", "2025-13", "May", "2025-06..2025-01", "2026-11"])
def test_parse_months_refuses(spec):
    today = date(2026, 11, 1) if spec == "2026-11" else TODAY  # nothing of a month is over on its 1st
    with pytest.raises(ValueError):
        parse_months([spec], today)


def test_month_window_stops_before_today():
    start, end = month_window(date(2025, 5, 1), DHAKA, TODAY)
    assert (start, end) == (datetime(2025, 5, 1, tzinfo=DHAKA), datetime(2025, 6, 1, tzinfo=DHAKA))
    _, end = month_window(date(2026, 11, 1), DHAKA, TODAY)
    assert end == datetime(2026, 11, 19, tzinfo=DHAKA)  # the daily run covers today


# -- layouts -----------------------------------------------------------------

def _children(*locs):
    return [(loc, None) for loc in locs]


def test_layout_daily_with_query_and_percent():
    layout = learn_layout("x", _children(
        "https://a.com/sitemap/sitemap-daily-2026-10-22.xml",
        "https://a.com/sitemap/sitemap-daily-2026-10-21.xml",
        "https://a.com/sitemap/sitemap-static-pages.xml",
        "https://a.com/sitemaps/sitemap-news-2026-11-07.xml?path=0001%2F",
    ))
    assert layout.kind == "daily"
    days = {datetime(2025, 5, 1).strftime(t) for t in layout.daily}
    assert days == {"https://a.com/sitemap/sitemap-daily-2025-05-01.xml",
                    "https://a.com/sitemaps/sitemap-news-2025-05-01.xml?path=0001%2F"}


def test_layout_monthly_yearly_and_wordpress():
    monthly = learn_layout("x", _children("https://a.com/2026-10-01.xml", "https://a.com/2026-11-01.xml",
                                          "https://a.com/page.xml"))
    assert monthly.kind == "monthly"
    assert datetime(2025, 5, 1).strftime(next(iter(monthly.monthly))) == "https://a.com/2025-05-01.xml"
    dated_dirs = learn_layout("x", _children("https://a.com/daily-sitemap/2026-11-19/sitemap.xml",
                                             "https://a.com/daily-sitemap/2026-11-18/sitemap.xml"))
    assert dated_dirs.kind == "daily"
    yearly = learn_layout("x", _children("https://a.com/sitemaps/2026/sitemap-2026-1.xml",
                                         "https://a.com/sitemaps/2025/sitemap-2025-2.xml"))
    assert yearly.yearly == {"https://a.com/sitemaps/{year}/sitemap-{year}-1.xml",
                             "https://a.com/sitemaps/{year}/sitemap-{year}-2.xml"}
    wordpress = learn_layout("x", _children("https://a.com/post-sitemap.xml", "https://a.com/post-sitemap2.xml",
                                            "https://a.com/post_tag-sitemap3.xml", "https://a.com/author-sitemap.xml"))
    assert wordpress.kind == "lastmod"
    assert [loc for loc, _ in wordpress.undated] == ["https://a.com/post-sitemap.xml", "https://a.com/post-sitemap2.xml"]
    assert learn_layout("x", []).kind == "none"


def test_headline_text():
    def has(url, title=""):
        return headline_text(Candidate(url=url, source_id="x", title=title))
    assert has("https://a.com/business/news/3-banks-get-emergency-funds-3789026")
    assert has("https://a.com/capital/article/293350/" + quote("ঢাকায়-পুলিশ-সদস্য-হত্যা"))
    assert not has("https://www.prothomalo.com/bangladesh/uyrdgbm7hw")
    assert not has("https://www.atnnewstv.com/details/15981")
    assert has("https://www.atnnewstv.com/details/15981", title="সীমান্তে যুবক নিহত")


def test_is_locked(tmp_path):
    path = tmp_path / ".run.lock"
    assert not is_locked(path)
    with run_lock(path):
        assert is_locked(path)
    assert not is_locked(path)


# -- end to end --------------------------------------------------------------

def _article(title: str, body: str, published: str) -> str:
    return (f'<html><head><meta property="article:published_time" content="{published}"></head><body><article>'
            f"<h1>{title}</h1><p>{body}</p><p>{body}</p></article></body></html>")


BORDER = "সীমান্তে বিএসএফের গুলিতে যুবক নিহত"
BORDER_BODY = "লালমনিরহাটের পাটগ্রাম সীমান্তে বিএসএফের গুলিতে এক বাংলাদেশি যুবক নিহত হয়েছেন বলে জানিয়েছে বিজিবি।"
CUSTODY = "আটকের পর পায়ে গুলি, পুলিশ হেফাজতে মৃত্যু"
CUSTODY_BODY = "পুলিশ হেফাজতে নির্যাতনে এক যুবকের মৃত্যুর অভিযোগ করেছে পরিবার। ক্রসফায়ারে নিহতের ঘটনাও তদন্তের দাবি।"
SLUG = quote("সীমান্তে-বিএসএফের-গুলিতে-যুবক-নিহত")


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


def _urlset(*locs: str) -> str:
    urls = "".join(f"<url><loc>{SITE}{loc}</loc><lastmod>2025-05-03T10:00:00+06:00</lastmod></url>" for loc in locs)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


def make_archive_site(requests: Counter):
    """The index lists only recent daily files; May 2025 is reached through the pattern."""
    pages = {
        "/robots.txt": ("text/plain", f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n"),
        "/sitemap.xml": ("application/xml",
                         '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                         f"<sitemap><loc>{SITE}/sitemap/sitemap-daily-2026-11-18.xml</loc></sitemap>"
                         f"<sitemap><loc>{SITE}/sitemap/sitemap-daily-2026-11-17.xml</loc></sitemap>"
                         f"<sitemap><loc>{SITE}/sitemap/sitemap-static-pages.xml</loc></sitemap>"
                         "</sitemapindex>"),
        # headline slug with keywords | id only (no headline) | slug without keywords
        "/sitemap/sitemap-daily-2025-05-03.xml": ("application/xml", _urlset(
            f"/national/article/101/{SLUG}", "/bangladesh/k3j9x2", "/sports/article/103/cricket-team-wins-series",
            "/video/hlvbbf8n0b")),
        f"/national/article/101/{SLUG}": ("text/html", _article(BORDER, BORDER_BODY, "2025-05-03T10:00:00+06:00")),
        "/bangladesh/k3j9x2": ("text/html", _article(CUSTODY, CUSTODY_BODY, "2025-05-03T12:00:00+06:00")),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?")[0]
        requests[path] += 1
        if path in pages:
            kind, text = pages[path]
            return httpx.Response(200, stream=_Stream(text.encode()), headers={"Content-Type": f"{kind}; charset=utf-8"})
        return httpx.Response(404, stream=_Stream(b"no"))
    return httpx.MockTransport(handler)


def make_config(tmp_path, keywords, untitled="download") -> Config:
    settings = {
        "timezone": "Asia/Dhaka", "data_dir": str(tmp_path),
        "http": {"per_host_delay": 0, "per_host_jitter": 0, "retries": 0, "respect_robots": True},
        "backfill": {"per_host_delay": 0, "untitled": untitled},
    }
    source = Source(id="fake", name="Fake Daily", language="bn", type="national", homepage=SITE + "/")
    return Config(settings=settings, sources=[source], keywords=keywords, config_dir=tmp_path)


@pytest.mark.anyio
async def test_backfill_month_end_to_end(tmp_path, cfg, database):
    config = make_config(tmp_path, cfg.keywords)
    requests: Counter = Counter()
    gate = RunGate(tmp_path / ".run.lock")
    opts = BackfillOptions(months=[date(2025, 5, 1)])

    # A daily run once skipped the custody article as too old; the backfill must still take it.
    storage = await PostgresStorage.open(tmp_path)
    from avn_scraper.scraper.urls import article_id
    await storage.mark_seen([(article_id(f"{SITE}/bangladesh/k3j9x2"), f"{SITE}/bangladesh/k3j9x2", "old")])
    await storage.close()

    [result] = await run_backfill(config, opts, transport=make_archive_site(requests), gate=gate)
    assert {r["title"] for r in result.saved} == {BORDER, CUSTODY}
    assert {r["published_date"] for r in result.saved} == {"2025-05-03"}
    assert requests["/sports/article/103/cricket-team-wins-series"] == 0  # no keyword in its headline
    assert requests["/video/hlvbbf8n0b"] == 0  # untitled, but a video: nothing to read
    assert sum(requests[f"/sitemap/sitemap-daily-2025-05-{d:02d}.xml"] for d in range(1, 32)) == 31
    src = result.sources["fake"]
    assert (src["status"], src["layout"], src["candidates"], src["untitled"], src["saved"]) == \
        ("done", "daily", 4, 2, 2)

    storage = await PostgresStorage.open(tmp_path)
    try:
        [row] = await storage.backfill_progress([date(2025, 5, 1)])
        assert (row["status"], row["saved"]) == ("done", 2)
        assert await storage.last_full_run() is None  # a backfill is not the daily run
        runs = await storage.query_df("SELECT stats->'backfill'->>'month' AS month FROM runs")
        assert list(runs["month"]) == ["2025-05"]
    finally:
        await storage.close()

    # Run again: the month is done, nothing is requested.
    requests.clear()
    [again] = await run_backfill(config, opts, transport=make_archive_site(requests), gate=gate)
    assert again.skipped and not requests


@pytest.mark.anyio
async def test_backfill_untitled_skip_and_dry_run(tmp_path, cfg, database):
    config = make_config(tmp_path, cfg.keywords, untitled="skip")
    requests: Counter = Counter()
    opts = BackfillOptions(months=[date(2025, 5, 1)], dry_run=True)
    [result] = await run_backfill(config, opts, transport=make_archive_site(requests),
                                  gate=RunGate(tmp_path / ".run.lock"))
    assert [r["title"] for r in result.saved] == [BORDER]
    assert requests["/bangladesh/k3j9x2"] == 0
    storage = await PostgresStorage.open(tmp_path)
    try:
        assert await storage.backfill_progress() == []  # a dry run records nothing
        assert (await storage.query_df("SELECT count(*) AS n FROM articles"))["n"][0] == 0
    finally:
        await storage.close()


@pytest.mark.anyio
async def test_gate_waits_for_the_daily_run(tmp_path):
    import asyncio
    lock = tmp_path / ".run.lock"
    gate = RunGate(lock, poll_seconds=0.05)
    with run_lock(lock):
        waiter = asyncio.create_task(gate.wait())
        await asyncio.sleep(0.2)
        assert not waiter.done()  # paused while the scrape holds the lock
    await asyncio.wait_for(waiter, 1)


def test_backfill_cli_status_and_bad_month(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from avn_scraper.cli import app
    result = CliRunner().invoke(app, ["backfill", "2099-01"])
    assert result.exit_code == 2 and "future" in result.output
    result = CliRunner().invoke(app, ["backfill"])
    assert result.exit_code == 2


def test_utc_window_is_dhaka_month():
    start, _ = month_window(date(2025, 5, 1), DHAKA, TODAY)
    assert start.astimezone(timezone.utc) == datetime(2025, 4, 30, 18, tzinfo=timezone.utc)


class _FakeFetcher:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.asked: list[str] = []

    async def get(self, url, purpose="page"):
        self.asked.append(url)
        if url not in self.files:
            return None
        return type("R", (), {"content": self.files[url].encode()})()


def _wp_urlset(*items: tuple[str, str]) -> str:
    urls = "".join(f"<url><loc>https://a.com/{slug}/</loc><lastmod>{when}</lastmod></url>" for slug, when in items)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


@pytest.mark.anyio
async def test_wordpress_sitemaps_read_newest_first_and_stop():
    from avn_scraper.scraper.discover.archive import ArchiveReader
    files = {
        "https://a.com/post-sitemap3.xml": _wp_urlset(("may-news", "2025-05-10T10:00:00+06:00"),
                                                      ("june-news", "2025-06-20T10:00:00+06:00")),
        "https://a.com/post-sitemap2.xml": _wp_urlset(("april-news", "2025-04-10T10:00:00+06:00")),
        "https://a.com/post-sitemap.xml": _wp_urlset(("old-news", "2020-01-01T10:00:00+06:00")),
    }
    layout = learn_layout("x", _children("https://a.com/post-sitemap.xml", "https://a.com/post-sitemap2.xml",
                                         "https://a.com/post-sitemap3.xml"))
    fetcher = _FakeFetcher(files)
    start, end = month_window(date(2025, 5, 1), DHAKA, TODAY)
    source = Source(id="x", name="X", language="bn", type="national", homepage="https://a.com/")
    result = await ArchiveReader(fetcher).month(source, layout, start, end)
    # June is kept as a maybe (lastmod is an edit time); the article's own date decides later.
    assert sorted(c.url for c in result.candidates) == ["https://a.com/june-news/", "https://a.com/may-news/"]
    assert "https://a.com/post-sitemap.xml" not in fetcher.asked  # stopped at the April file
    assert not result.undated


@pytest.mark.anyio
async def test_undated_sitemaps_are_skipped_after_one_file():
    """A local site whose sitemap entries have no lastmod: no month can be picked, so the
    series is not read further and the outlet is marked no_archive."""
    from avn_scraper.scraper.discover.archive import ArchiveReader

    def plain(*slugs):
        urls = "".join(f"<url><loc>https://a.com/{s}/</loc></url>" for s in slugs)
        return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'

    files = {f"https://a.com/post-sitemap{n}.xml": plain(f"story-{n}") for n in ("", "2", "3")}
    layout = learn_layout("x", _children(*files))
    fetcher = _FakeFetcher(files)
    start, end = month_window(date(2025, 5, 1), DHAKA, TODAY)
    source = Source(id="x", name="X", language="bn", type="regional", homepage="https://a.com/")
    result = await ArchiveReader(fetcher).month(source, layout, start, end)
    assert result.undated and result.candidates == []
    assert fetcher.asked == ["https://a.com/post-sitemap3.xml"]
