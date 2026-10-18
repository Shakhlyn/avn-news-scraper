import asyncio
import gzip

import httpx
import pandas as pd
import pytest

from avn_scraper.discover import sitemap
from avn_scraper.discover.catchall import unwrap_bing_link
from avn_scraper.export import _neutralize
from avn_scraper.fetch import Fetcher

SETTINGS = {"respect_robots": False, "per_host_delay": 0, "retries": 0}


def _fetch(handler, url, **settings):
    async def go():
        async with Fetcher({**SETTINGS, **settings}, transport=httpx.MockTransport(handler)) as f:
            return await f.get(url), f.stats
    return asyncio.run(go())


def test_formula_cells_neutralized():
    df = pd.DataFrame({"title": ["=HYPERLINK(\"http://x\")", "+1", "-2 dead", "@SUM(A1)", "normal", "সীমান্তে"],
                       "score": [1, 2, 3, 4, 5, 6]}).map(_neutralize)
    assert df["title"].tolist() == ["'=HYPERLINK(\"http://x\")", "'+1", "'-2 dead", "'@SUM(A1)", "normal", "সীমান্তে"]
    assert df["score"].tolist() == [1, 2, 3, 4, 5, 6]


def test_gzip_bomb_rejected(monkeypatch):
    monkeypatch.setattr(sitemap, "_MAX_XML_BYTES", 1000)
    bomb = gzip.compress(b"<urlset>" + b" " * 100_000 + b"</urlset>")
    with pytest.raises(ValueError):
        sitemap.parse_sitemap(bomb)


def test_small_gzip_sitemap_ok():
    xml = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://x.com/1</loc></url></urlset>'
    assert sitemap.parse_sitemap(gzip.compress(xml))[1][0]["loc"] == "https://x.com/1"


def test_unwrap_bing_requires_real_bing_host():
    assert unwrap_bing_link("http://evilbing.com/a?url=http%3a%2f%2f10.0.0.1%2f") == "http://evilbing.com/a?url=http%3a%2f%2f10.0.0.1%2f"
    assert unwrap_bing_link("http://www.bing.com/a?url=file%3a%2f%2f%2fetc%2fpasswd").startswith("http://www.bing.com")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                                 "http://10.0.0.5/admin", "http://[::1]/"])
def test_private_addresses_blocked(url):
    resp, stats = _fetch(lambda req: httpx.Response(200, text="secret"), url)
    assert resp is None and stats["BlockedURL"] == 1


def test_redirect_to_private_address_blocked():
    def handler(req):
        if req.url.host == "93.184.215.14":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="secret")
    resp, stats = _fetch(handler, "http://93.184.215.14/article")
    assert resp is None and stats["BlockedURL"] == 1


def test_public_address_fetched_and_decoded():
    body = gzip.compress("<html>খবর</html>".encode())
    handler = lambda req: httpx.Response(200, content=body, headers={"Content-Encoding": "gzip",
                                                                     "Content-Type": "text/html; charset=utf-8"})
    resp, _ = _fetch(handler, "http://93.184.215.14/a")
    assert resp is not None and resp.text == "<html>খবর</html>" and str(resp.url) == "http://93.184.215.14/a"


def test_oversized_response_dropped():
    resp, stats = _fetch(lambda req: httpx.Response(200, content=b"x" * 3_000_000), "http://93.184.215.14/big",
                         max_response_mb=1)
    assert resp is None and stats["ResponseTooLarge"] == 1
