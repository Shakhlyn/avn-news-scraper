"""End-to-end run against a fake news site served by httpx.MockTransport."""

import asyncio
import json
from collections import Counter
from datetime import datetime, timezone

import httpx
import pandas as pd

from avn_scraper.config import Config, Source
from avn_scraper.pipeline import RunOptions, run_pipeline
from avn_scraper.storage import LocalStorage

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
        "timezone": "Asia/Dhaka", "data_dir": str(tmp_path), "window_hours": 30,
        "http": {"per_host_delay": 0, "retries": 0, "respect_robots": True},
        "max_articles_per_source": 80,
    }
    source = Source(id="fake", name="Fake Daily", language="bn", type="national", homepage=SITE + "/")
    return Config(settings=settings, sources=[source], keywords=keywords, config_dir=tmp_path)


def test_end_to_end_run(tmp_path, cfg):
    config = make_config(tmp_path, cfg.keywords)
    requests: Counter = Counter()

    result = asyncio.run(run_pipeline(config, RunOptions(), transport=make_site(requests)))
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

    # stored: JSON per article, raw HTML for the downloaded one, DB rows, exports
    storage = LocalStorage(tmp_path)
    rows = storage.query_df("SELECT * FROM v_matches")
    assert set(rows["url"]) == {SITE + "/news/1", SITE + "/news/2"}
    paths = storage.query_df("SELECT json_path, raw_path FROM articles WHERE url = ?", (SITE + "/news/1",)).iloc[0]
    storage.close()
    record = json.loads((tmp_path / paths["json_path"]).read_text(encoding="utf-8"))
    assert record["title"] == full["title"] and (tmp_path / paths["raw_path"]).exists()
    assert (tmp_path / "exports" / "master.xlsx").exists()
    assert len(pd.read_csv(next((tmp_path / "exports" / "daily").glob("*.csv")))) == 2

    # second run: only the temporarily failed article is tried again
    requests.clear()
    second = asyncio.run(run_pipeline(config, RunOptions(), transport=make_site(requests)))
    assert requests["/news/1"] == 0 and requests["/news/2"] == 0 and requests["/news/3"] == 1
    assert second.stats["outcome"].get("saved", 0) == 0
