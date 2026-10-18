"""Polite async HTTP client: per-site pacing, robots.txt, retries.

URLs come from untrusted pages (feed items, search results, redirects), so every
request - including each redirect hop - must go to a public address, and bodies
are capped in size while streaming (this also bounds gzip Content-Encoding).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections import Counter, defaultdict
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

_DROP_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


class BlockedURL(Exception):
    """Non-http(s) URL or one that resolves to a private/loopback/link-local address."""


class ResponseTooLarge(Exception):
    pass


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


class Fetcher:
    def __init__(self, http_settings: dict, transport: httpx.AsyncBaseTransport | None = None):
        s = http_settings
        self.user_agent = s.get("user_agent", "AVNNewsScraper/0.1")
        self.robots_agent = s.get("robots_agent", "AVNNewsScraper")
        self.per_host_delay = float(s.get("per_host_delay", 1.0))
        self.retries = int(s.get("retries", 2))
        self.respect_robots = bool(s.get("respect_robots", True))
        self.max_bytes = int(float(s.get("max_response_mb", 20)) * 1024 * 1024)
        self._public_hosts: dict[str, bool] = {}
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self.user_agent,
                "Accept-Language": "bn-BD,bn;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=float(s.get("timeout_seconds", 25)),
            follow_redirects=True,
            event_hooks={"request": [self._check_target]},
            transport=transport,
        )
        self._sem = asyncio.Semaphore(int(s.get("concurrency", 12)))
        self._host_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._host_last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.stats: Counter[str] = Counter()

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _check_target(self, request: httpx.Request) -> None:
        """httpx request hook: runs for the first request and for every redirect."""
        if request.url.scheme not in ("http", "https"):
            raise BlockedURL(str(request.url))
        host = request.url.host
        if host not in self._public_hosts:
            try:
                ipaddress.ip_address(host)
                public = _is_public_ip(host)
            except ValueError:
                try:
                    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
                    public = all(_is_public_ip(info[4][0]) for info in infos)
                except OSError:
                    public = True  # unresolvable: let the request fail normally
            self._public_hosts[host] = public
        if not self._public_hosts[host]:
            raise BlockedURL(str(request.url))

    async def _get_capped(self, url: str) -> httpx.Response:
        async with self._client.stream("GET", url) as r:
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > self.max_bytes:
                    raise ResponseTooLarge(url)
            # aiter_bytes already decoded the body; drop the encoding headers to match.
            headers = [(k, v) for k, v in r.headers.multi_items() if k.lower() not in _DROP_HEADERS]
            return httpx.Response(r.status_code, headers=headers, content=bytes(body), request=r.request)

    async def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_locks[origin]:
            if origin not in self._robots:
                parser: RobotFileParser | None = None
                resp = await self._request(f"{origin}/robots.txt")
                if resp is not None and resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                self._robots[origin] = parser  # None = no robots.txt = allowed
            return self._robots[origin]

    async def robots_sitemaps(self, url: str) -> list[str]:
        parser = await self._robots_for(url)
        return list(parser.site_maps() or []) if parser else []

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = await self._robots_for(url)
        return parser is None or parser.can_fetch(self.robots_agent, url)

    async def _request(self, url: str) -> httpx.Response | None:
        host = urlsplit(url).netloc
        for attempt in range(self.retries + 1):
            async with self._host_locks[host]:
                wait = self._host_last.get(host, 0) + self.per_host_delay - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    async with self._sem:
                        resp = await self._get_capped(url)
                except (BlockedURL, ResponseTooLarge) as exc:
                    self.stats[type(exc).__name__] += 1
                    log.warning("not fetching %s: %s", url, type(exc).__name__)
                    return None
                except httpx.HTTPError as exc:
                    resp = None
                    log.debug("fetch error %s: %r", url, exc)
                finally:
                    self._host_last[host] = time.monotonic()
            if resp is not None and resp.status_code < 500 and resp.status_code != 429:
                return resp
            if attempt < self.retries:
                await asyncio.sleep(2 * (attempt + 1))
        return resp

    async def get(self, url: str) -> httpx.Response | None:
        """GET url; None when blocked by robots.txt, on network error or non-2xx status."""
        if not await self.allowed(url):
            self.stats["robots_blocked"] += 1
            log.info("robots.txt disallows %s", url)
            return None
        resp = await self._request(url)
        if resp is None:
            self.stats["errors"] += 1
            return None
        if resp.status_code >= 400:
            self.stats[f"http_{resp.status_code}"] += 1
            log.debug("HTTP %s for %s", resp.status_code, url)
            return None
        self.stats["ok"] += 1
        return resp
