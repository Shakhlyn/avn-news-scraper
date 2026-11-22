"""End-to-end run against a fake news site served by httpx.MockTransport."""

import asyncio
from collections import Counter
from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest

from avn_scraper.scraper.config import Config, Source
from avn_scraper.scraper.discover import Candidate
from avn_scraper.scraper.pipeline import RunOptions, _cap_per_source, run_pipeline
from avn_scraper.scraper.storage import PostgresStorage

SITE = "http://93.184.215.14"  # public IP literal: passes the SSRF guard without DNS
NOW = datetime.now(timezone.utc).isoformat()

HOMEPAGE = f"""<html><body>
<a href="/news/1">সীমান্তে বিএসএফের গুলিতে যুবক নিহত</a>
<a href="/news/2">Youth shot dead by BSF at Lalmonirhat border</a>
<a href="/news/3">আটকের পর পায়ে গুলি করার অভিযোগ পরিবারের</a>
<a href="/news/4">Stock market gains for the third straight day</a>
</body></html>"""

ARTICLE_1 = f"""<html><head><meta property="article:published_time" content="{NOW}"></head><body><article>
<h1>সীমান্তে বিএসএফের গুলিতে যুবক নিহত</h1>
<p>লালমনিরহাটের পাটগ্রাম সীমান্তে বিএসএফের গুলিতে এক বাংলাদেশি যুবক নিহত হয়েছেন বলে জানিয়েছে বিজিবি।</p>
<p>নিহতের পরিবার এ ঘটনার বিচার দাবি করেছে। স্থানীয়রা জানান, ভোরে এ ঘটনা ঘটে।</p>
</article></body></html>"""


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


def make_site(requests: Counter):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests[path] += 1
        html = {"/": HOMEPAGE, "/news/1": ARTICLE_1}.get(path)
        if html is not None:
            return httpx.Response(200, stream=_Stream(html.encode()),
                                  headers={"Content-Type": "text/html; charset=utf-8"})
        status = {"/news/2": 403, "/news/3": 503}.get(path, 404)
        return httpx.Response(status, stream=_Stream(b"no"))
    return httpx.MockTransport(handler)


def make_config(tmp_path, keywords) -> Config:
    settings = {
        "timezone": "Asia/Dhaka", "data_dir": str(tmp_path), "window_hours": 24,
        "http": {"per_host_delay": 0, "retries": 0, "respect_robots": True},
    }
    source = Source(id="fake", name="Fake Daily", language="bn", type="national", homepage=SITE + "/")
    return Config(settings=settings, sources=[source], keywords=keywords, config_dir=tmp_path)


@pytest.mark.anyio
async def test_end_to_end_run(tmp_path, cfg, database):
    config = make_config(tmp_path, cfg.keywords)
    requests: Counter = Counter()

    result = await run_pipeline(config, RunOptions(prefilter=True), transport=make_site(requests))
    out = result.stats["outcome"]

    # 1: full article saved; 2: blocked (403) -> saved from its headline; 3: 503 -> retry later;
    # 4: no keyword in the headline -> never downloaded
    assert out["saved"] == 2 and out["saved_headline_only"] == 1 and out["fetch_failed"] == 1
    assert requests["/news/4"] == 0
    by_url = {r["url"]: r for r in result.saved}
    full, headline_only = by_url[SITE + "/news/1"], by_url[SITE + "/news/2"]
    assert full["categories"][0]["category"] == "border_killing" and full["body"]
    assert full["district"] == "Lalmonirhat"
    assert headline_only["body_missing"] and headline_only["categories"][0]["category"] == "border_killing"
    assert {f["reason"] for f in result.stats["fetch_failed"]} == {"http_403", "http_503"}

    # stored: DB rows, raw HTML for the downloaded one, exports
    storage = await PostgresStorage.open(tmp_path)
    rows = await storage.query_df("SELECT * FROM v_matches")
    assert set(rows["url"]) == {SITE + "/news/1", SITE + "/news/2"}
    stored = (await storage.query_df("SELECT title, raw_path FROM articles WHERE url = %s", (SITE + "/news/1",))).iloc[0]
    await storage.close()
    assert stored["title"] == full["title"] and (tmp_path / stored["raw_path"]).exists()
    assert (tmp_path / "exports" / "master.xlsx").exists()
    assert len(pd.read_csv(next((tmp_path / "exports" / "daily").glob("*.csv")))) == 2

    # second run: only the temporarily failed article is tried again
    requests.clear()
    second = await run_pipeline(config, RunOptions(prefilter=True), transport=make_site(requests))
    assert requests["/news/1"] == 0 and requests["/news/2"] == 0 and requests["/news/3"] == 1
    assert second.stats["outcome"].get("saved", 0) == 0


TAB_HOME = """<html><body><nav><a href="/">Home</a><a href="/national">জাতীয়</a></nav>
<a href="/news/9">Stock market gains for the third straight day</a></body></html>"""
TAB_PAGE_1 = """<html><body><nav><a href="/national">জাতীয়</a></nav>
<a href="/news/5">সীমান্তে বিএসএফের গুলিতে যুবক নিহত</a>
<a href="/news/7">Stock market falls for the second straight day</a>
<a href="/national?page=2">2</a></body></html>"""
TAB_PAGE_2 = """<html><body><a href="/news/6">Youth shot dead by BSF at Lalmonirhat border</a>
<a href="/national?page=3">3</a></body></html>"""


def make_tab_site(requests: Counter):
    pages = {"/": TAB_HOME, "/national": TAB_PAGE_1, "/national?page=2": TAB_PAGE_2,
             "/national?page=3": TAB_PAGE_2, "/news/5": ARTICLE_1, "/news/6": ARTICLE_1.replace("লালমনিরহাটের", "সাতক্ষীরার")}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.raw_path.decode()
        requests[key] += 1
        html = pages.get(key)
        if html is None:
            return httpx.Response(404, stream=_Stream(b"no"))
        return httpx.Response(200, stream=_Stream(html.encode()), headers={"Content-Type": "text/html; charset=utf-8"})
    return httpx.MockTransport(handler)


def test_section_tabs_and_pagination(tmp_path, cfg, database):
    config = make_config(tmp_path, cfg.keywords)
    requests: Counter = Counter()

    result = asyncio.run(run_pipeline(config, RunOptions(), transport=make_tab_site(requests)))
    counts = result.stats["sources"]["fake"]["counts"]
    assert result.stats["sources"]["fake"]["endpoints"]["sections"] == [SITE + "/national"]
    # page 3 repeats page 2's links, so paging stops there
    assert counts["section_pages"] == 3 and requests["/national?page=3"] == 1
    assert {r["url"] for r in result.saved} == {SITE + "/news/5", SITE + "/news/6"}

    # next run: page 1 holds only links seen last time, so page 2 isn't requested
    requests.clear()
    second = asyncio.run(run_pipeline(config, RunOptions(), transport=make_tab_site(requests)))
    assert requests["/national"] == 1 and requests["/national?page=2"] == 0
    assert second.stats["sources"]["fake"]["counts"]["section_pages"] == 1


NO_KEYWORD_HOME = f"""<html><body>
<a href="/news/5">Family demands justice for Patgram youth</a>
<a href="/news/6">Stock market gains for the third straight day</a>
</body></html>"""

ARTICLE_5 = ARTICLE_1.replace("সীমান্তে বিএসএফের গুলিতে যুবক নিহত</h1>", "Family demands justice for Patgram youth</h1>")
ARTICLE_6 = f"""<html><head><meta property="article:published_time" content="{NOW}"></head><body><article>
<h1>Stock market gains for the third straight day</h1>
<p>Shares rose on the Dhaka Stock Exchange for a third day as banks and telecoms led the gains.</p>
<p>Turnover also increased, brokers said, with investors returning after the holiday.</p>
</article></body></html>"""


def make_plain_headline_site(requests: Counter):
    pages = {"/": NO_KEYWORD_HOME, "/news/5": ARTICLE_5, "/news/6": ARTICLE_6}

    def handler(request: httpx.Request) -> httpx.Response:
        requests[request.url.path] += 1
        html = pages.get(request.url.path)
        if html is None:
            return httpx.Response(404, stream=_Stream(b"no"))
        return httpx.Response(200, stream=_Stream(html.encode()),
                              headers={"Content-Type": "text/html; charset=utf-8"})
    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_default_downloads_every_article(tmp_path, cfg, database):
    """A relevant story with a plain headline is found from its body; the rest are judged once."""
    config = make_config(tmp_path, cfg.keywords)
    requests: Counter = Counter()

    result = await run_pipeline(config, RunOptions(), transport=make_plain_headline_site(requests))

    assert requests["/news/5"] == 1 and requests["/news/6"] == 1
    assert {r["url"] for r in result.saved} == {SITE + "/news/5"}
    assert result.saved[0]["categories"][0]["category"] == "border_killing"
    assert "capped" not in result.stats

    requests.clear()
    await run_pipeline(config, RunOptions(), transport=make_plain_headline_site(requests))
    assert requests["/news/5"] == 0 and requests["/news/6"] == 0  # not downloaded twice


def _cand(n: int, source: str = "a") -> Candidate:
    return Candidate(url=f"{SITE}/news/{n}", source_id=source,
                     published_at=datetime(2026, 11, 20, n, tzinfo=timezone.utc))


def test_cap_per_source():
    now = datetime(2026, 11, 20, 23, tzinfo=timezone.utc)
    items = [_cand(n) for n in range(5)] + [_cand(9, "b")]
    assert _cap_per_source(items, None, now) == (items, {})
    assert _cap_per_source(items, 0, now) == (items, {})
    selected, left_out = _cap_per_source(items, 2, now)
    assert [c.url for c in selected if c.source_id == "a"] == [f"{SITE}/news/4", f"{SITE}/news/3"]
    assert left_out == {"a": 3}


def test_progress_line_and_periodic_log(caplog):
    import logging
    from types import SimpleNamespace
    from avn_scraper.scraper.pipeline import Progress

    by_id = {"star": SimpleNamespace(type="national"), "tv": SimpleNamespace(type="tv"),
             "local": SimpleNamespace(type="regional")}
    hosts = {"star.com": "star", "tv.com": "tv", "local.com": "local"}
    fetcher = SimpleNamespace(requests_failed=12, stopped_hosts=lambda: {"a.com": "x"},
                              requests_by_host=lambda: {"star.com": 100, "tv.com": 20, "local.com:443": 900},
                              source_for=hosts.get)

    async def go():
        async with Progress(fetcher, every=0.02, by_id=by_id) as p:
            p.start_phase("outlets scanned", ["star", "tv", "local", "local"])
            await p.counted(asyncio.sleep(0), "star")
            await p.counted(asyncio.sleep(0), "local")
            await asyncio.sleep(0.07)
            return p.line()

    with caplog.at_level(logging.INFO, logger="avn_scraper.scraper.pipeline"):
        line = asyncio.run(go())
    assert line.startswith("progress: outlets scanned: national 1/2 (120 requests), "
                           "regional 1/2 (900 requests); 12 failed; 1 sites stopped; 0m0")
    assert sum("national 1/2" in r.message for r in caplog.records) >= 2


def test_progress_off_when_zero():
    from types import SimpleNamespace
    from avn_scraper.scraper.pipeline import Progress

    async def go():
        async with Progress(SimpleNamespace(), every=0) as p:
            return p._task
    assert asyncio.run(go()) is None


def test_run_past_max_run_minutes_is_stopped(monkeypatch, cfg):
    from avn_scraper.scraper import pipeline

    class Storage:
        closed = False

        async def close(self):
            Storage.closed = True

    async def fake_open(data_dir):
        return Storage()

    async def slow_run(*args):
        await asyncio.sleep(3600)

    monkeypatch.setattr(pipeline.PostgresStorage, "open", fake_open)
    monkeypatch.setattr(pipeline, "_run", slow_run)
    config = Config(**{**cfg.__dict__, "settings": {**cfg.settings, "max_run_minutes": 0.001}})
    with pytest.raises(pipeline.RunTooLong):
        asyncio.run(run_pipeline(config, RunOptions()))
    assert Storage.closed


def test_timeout_inside_a_run_is_not_reported_as_too_long(monkeypatch, cfg):
    from avn_scraper.scraper import pipeline

    class Storage:
        async def close(self):
            pass

    async def fake_open(data_dir):
        return Storage()

    async def failing_run(*args):
        raise TimeoutError("something else")

    monkeypatch.setattr(pipeline.PostgresStorage, "open", fake_open)
    monkeypatch.setattr(pipeline, "_run", failing_run)
    with pytest.raises(TimeoutError, match="something else"):
        asyncio.run(run_pipeline(cfg, RunOptions()))


@pytest.mark.anyio
async def test_articles_are_judged_as_they_download_not_all_at_the_end(tmp_path, cfg, database, monkeypatch):
    """Pages must not pile up in memory: the first article is judged before the last download ends."""
    from avn_scraper.scraper import pipeline
    events: list[str] = []
    real_fetch, real_article = pipeline._fetch_one, pipeline.Judge.article
    order: dict[str, int] = {}

    async def slow_fetch(fetcher, c):
        n = order.setdefault(c.url, len(order))
        await asyncio.sleep(0.1 * n)  # later articles take longer to download
        out = await real_fetch(fetcher, c)
        events.append("downloaded")
        return out

    async def article(self, c, html, ex, reason):
        events.append("judged")
        return await real_article(self, c, html, ex, reason)

    monkeypatch.setattr(pipeline, "_fetch_one", slow_fetch)
    monkeypatch.setattr(pipeline.Judge, "article", article)
    result = await run_pipeline(make_config(tmp_path, cfg.keywords), RunOptions(), transport=make_site(Counter()))
    assert events.count("downloaded") >= 3 and result.stats["outcome"]
    assert events.index("judged") < len(events) - 1 - events[::-1].index("downloaded")
