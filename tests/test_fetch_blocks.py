"""Block protection in the fetcher: circuit breaker, cooldowns, Retry-After, impersonation."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx

from avn_scraper.scraper import fetch
from avn_scraper.scraper.fetch import Fetcher

SETTINGS = {"respect_robots": False, "per_host_delay": 0, "per_host_jitter": 0, "retries": 0,
            "impersonate_delay": 0, "block_threshold": 3}
CHALLENGE = b"<html><head><title>Just a moment...</title></head><body>checking</body></html>"


class _Stream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


def _streaming(handler):
    """Re-wrap the handler's response with an unread body, like a real network response."""
    def wrapped(request):
        r = handler(request)
        return httpx.Response(r.status_code, headers=r.headers, stream=_Stream(r.content))
    return wrapped


def _run(coro_fn, handler=None, settings=None, **kwargs):
    async def go():
        transport = httpx.MockTransport(_streaming(handler)) if handler else None
        async with Fetcher({**SETTINGS, **(settings or {})}, transport=transport, **kwargs) as f:
            return await coro_fn(f)
    return asyncio.run(go())


def _no_sleep(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(fetch.asyncio, "sleep", fake_sleep)
    return slept


def test_breaker_stops_host_after_repeated_challenges(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(403, headers={"cf-mitigated": "challenge"}, content=CHALLENGE)

    async def go(f):
        return [await f.get(f"https://93.184.216.34/{i}") for i in range(10)], f
    results, f = _run(go, handler, cooldown_path=tmp_path / "blocks.json")
    assert results == [None] * 10
    assert len(calls) == 3  # the 4th..10th requests were never sent
    assert f.failures["https://93.184.216.34/0"] == "blocked_challenge"
    assert f.failures["https://93.184.216.34/9"] == "host_blocked"
    saved = json.loads((tmp_path / "blocks.json").read_text())
    assert "93.184.216.34" in saved and "challenge" in saved["93.184.216.34"]["reason"]


def test_cooldown_skips_host_on_next_run(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    path = tmp_path / "blocks.json"
    _run(lambda f: f._stop_host("93.184.216.34", "test"), cooldown_path=path)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, content=b"<html>ok</html>")

    async def go(f):
        return await f.get("https://93.184.216.34/a"), await f.get("https://1.1.1.1/b"), f.stopped_hosts()
    blocked, other, stopped = _run(go, handler, cooldown_path=path)
    assert blocked is None and other is not None
    assert len(calls) == 1  # only the other host was asked
    assert "93.184.216.34" in stopped


def test_expired_cooldown_is_dropped(tmp_path):
    path = tmp_path / "blocks.json"
    path.write_text(json.dumps({"93.184.216.34": {"until": "2020-01-01T00:00:00+00:00", "reason": "old"}}))
    cd = fetch.HostCooldowns(path)
    cd.load()
    assert cd.entries == {}


def test_success_resets_strikes(monkeypatch):
    _no_sleep(monkeypatch)
    answers = iter([403, 403, 200, 403, 403, 200])

    def handler(request):
        return httpx.Response(next(answers), content=b"<html>page</html>")

    async def go(f):
        out = [await f.get(f"https://93.184.216.34/{i}") for i in range(6)]
        return out, f.stopped_hosts()
    out, stopped = _run(go, handler)
    assert out[2] is not None and out[5] is not None
    assert stopped == {}


def test_plain_403_keeps_permanent_reason(monkeypatch):
    _no_sleep(monkeypatch)

    async def go(f):
        await f.get("https://93.184.216.34/x")
        return f.failures
    failures = _run(go, lambda r: httpx.Response(403, content=b"<html>forbidden page</html>" * 3000))
    assert failures["https://93.184.216.34/x"] == "http_403"


def test_challenge_page_with_200_is_not_an_article(monkeypatch):
    _no_sleep(monkeypatch)

    async def go(f):
        return await f.get("https://93.184.216.34/x"), f.failures
    resp, failures = _run(go, lambda r: httpx.Response(200, content=b"<title>Bot Verification</title>"))
    assert resp is None and failures["https://93.184.216.34/x"] == "blocked_challenge"


def test_big_page_mentioning_challenge_is_kept(monkeypatch):
    _no_sleep(monkeypatch)
    page = b"<html><head><title>News</title></head><body>" + b"just a moment " * 5000 + b"</body></html>"

    async def go(f):
        return await f.get("https://93.184.216.34/x")
    assert _run(go, lambda r: httpx.Response(200, content=page)) is not None


def test_retry_after_is_honoured(monkeypatch):
    slept = _no_sleep(monkeypatch)
    answers = iter([httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200, content=b"<html>ok</html>")])

    async def go(f):
        return await f.get("https://93.184.216.34/x")
    resp = _run(go, lambda r: next(answers), settings={"retries": 1})
    assert resp is not None
    assert any(s >= 29 for s in slept)  # waited out Retry-After before retrying


def test_long_retry_after_stops_host(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async def go(f):
        return [await f.get(f"https://93.184.216.34/{i}") for i in range(3)], f.stopped_hosts()
    out, stopped = _run(go, handler, settings={"retries": 2}, cooldown_path=tmp_path / "b.json")
    assert len(calls) == 1 and "93.184.216.34" in stopped


# --- impersonated (curl_cffi) path, with a fake session -----------------------------------

class _FakeCurlResponse:
    def __init__(self, status, body=b"", headers=None, ip="93.184.216.34"):
        self.status_code = status
        self._body = body
        self.headers = httpx.Headers(headers or {})
        self.primary_ip = ip

    async def aiter_content(self):
        for i in range(0, len(self._body), 1000):
            yield self._body[i:i + 1000]


class _FakeCurl:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        yield self.routes[url]


def _impersonated(routes, coro_fn, settings=None):
    curl = _FakeCurl(routes)
    src = SimpleNamespace(homepage="https://www.example-paper.com/", fetch="impersonate")
    out = _run(coro_fn, lambda r: httpx.Response(599), settings=settings, sources=[src], curl_session=curl)
    return out, curl


def _public_dns(monkeypatch):
    async def fake_getaddrinfo(self, host, port):
        return [(None, None, None, None, ("10.0.0.5" if "internal" in host else "93.184.216.34", 0))]
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)


def test_impersonated_host_goes_through_curl(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://www.example-paper.com/news/1"
    (resp, curl) = _impersonated({url: _FakeCurlResponse(200, b"<html>article</html>")},
                                 lambda f: f.get(url))
    assert resp.text == "<html>article</html>"
    assert curl.calls[0][1]["allow_redirects"] is False
    assert curl.calls[0][1]["headers"]["Referer"] == "https://www.example-paper.com/"


def test_impersonated_redirect_to_private_address_blocked(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://www.example-paper.com/r"
    routes = {url: _FakeCurlResponse(302, headers={"location": "http://internal.example/secret"})}

    async def go(f):
        return await f.get(url), f.failures
    (resp, failures), curl = _impersonated(routes, go)
    assert resp is None and failures[url] == "BlockedURL"
    assert len(curl.calls) == 1  # the private target was never requested


def test_impersonated_peer_address_checked(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://www.example-paper.com/x"

    async def go(f):
        return await f.get(url), f.failures
    (resp, failures), _ = _impersonated({url: _FakeCurlResponse(200, b"x", ip="127.0.0.1")}, go)
    assert resp is None and failures[url] == "BlockedURL"


def test_impersonated_size_cap(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://www.example-paper.com/big"

    async def go(f):
        return await f.get(url), f.failures
    (resp, failures), _ = _impersonated({url: _FakeCurlResponse(200, b"a" * 3_000_000)}, go,
                                        settings={"max_response_mb": 1})
    assert resp is None and failures[url] == "ResponseTooLarge"


def test_other_hosts_stay_on_plain_client():
    src = SimpleNamespace(homepage="https://www.example-paper.com/", fetch="impersonate")
    f = Fetcher(SETTINGS, sources=[src, SimpleNamespace(homepage="https://plain.com/", fetch="plain")])
    assert f.uses_impersonation("example-paper.com")
    assert f.uses_impersonation("bangla.example-paper.com")
    assert not f.uses_impersonation("plain.com")
    assert not f.uses_impersonation("notexample-paper.com")
