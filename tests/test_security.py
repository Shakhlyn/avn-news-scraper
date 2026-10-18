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


class _Stream(httpx.AsyncByteStream):
    """Unread streaming body, like a real network response."""

    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        for i in range(0, len(self.data), 65536):
            yield self.data[i:i + 65536]


def _streamed(data: bytes, **headers) -> httpx.Response:
    return httpx.Response(200, stream=_Stream(data), headers=headers)


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
    handler = lambda req: _streamed(body, **{"Content-Encoding": "gzip", "Content-Type": "text/html; charset=utf-8"})
    resp, _ = _fetch(handler, "http://93.184.215.14/a")
    assert resp is not None and resp.text == "<html>খবর</html>" and str(resp.url) == "http://93.184.215.14/a"


def test_oversized_response_dropped():
    resp, stats = _fetch(lambda req: _streamed(b"x" * 3_000_000), "http://93.184.215.14/big", max_response_mb=1)
    assert resp is None and stats["ResponseTooLarge"] == 1


def test_gzip_content_encoding_bomb_dropped():
    bomb = gzip.compress(b"\0" * 50_000_000)  # ~50 KB on the wire
    resp, stats = _fetch(lambda req: _streamed(bomb, **{"Content-Encoding": "gzip"}), "http://93.184.215.14/b",
                         max_response_mb=1)
    assert resp is None and stats["ResponseTooLarge"] == 1


def test_stacked_encoding_rejected():
    body = gzip.compress(gzip.compress(b"hello"))
    resp, stats = _fetch(lambda req: _streamed(body, **{"Content-Encoding": "gzip, gzip"}), "http://93.184.215.14/s")
    assert resp is None and stats["UnsupportedEncoding"] == 1


def test_unresolvable_host_blocked():
    resp, stats = _fetch(lambda req: _streamed(b"x"), "http://no-such-host.invalid/")
    assert resp is None and stats["BlockedURL"] == 1


def test_peer_address_checked_after_connect():
    """DNS rebinding: name looked public, but the socket connected to a private IP."""
    class FakeStream:
        def get_extra_info(self, key):
            return ("10.0.0.7", 80) if key == "server_addr" else None

    def handler(req):
        resp = _streamed(b"internal")
        resp.extensions["network_stream"] = FakeStream()
        return resp

    resp, stats = _fetch(handler, "http://93.184.215.14/rebind")
    assert resp is None and stats["BlockedURL"] == 1
