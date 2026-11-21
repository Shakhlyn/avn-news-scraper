"""Polite async HTTP client: per-site pacing, robots.txt, retries, block protection.

URLs come from untrusted pages (feed items, search results, redirects), so:
- every request, including each redirect hop, must target a public address
  (checked by name before sending, and again on the connected peer address,
  which defeats DNS rebinding);
- bodies are read raw and decompressed here with a size budget, so neither a
  large response nor a gzip bomb can exhaust memory.

A site that starts blocking us must never be pushed into banning our IP (a ban
would also stop every later test), so:
- requests to a site are spaced `per_host_delay` apart plus random jitter;
- a 429 `Retry-After` is honoured, and a site asking for a longer wait is stopped;
- after `block_threshold` block answers in a row (403, 429, a bot-challenge page)
  the site is not asked again this run, and it is put on a cooldown saved in
  `data/cache/host_blocks.json`, so later runs and `check-sources` skip it too.

Outlets whose bot protection rejects the TLS fingerprint of Python clients use
`fetch: impersonate` (sources.yaml): their requests go through curl_cffi with a
Chrome fingerprint, slower pacing, and the same address and size checks.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import random
import re
import time
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from curl_cffi.curl import CurlError
from curl_cffi.requests import AsyncSession

from .blocklog import BlockLog

log = logging.getLogger(__name__)

_DROP_HEADERS = ("content-encoding", "content-length", "transfer-encoding")
_MAX_REDIRECTS = 10
# Bot-check interstitials served instead of the page (Cloudflare, generic JS checks).
# Only small bodies are tested: real pages can carry Cloudflare's challenge scripts.
_CHALLENGE_TITLE = re.compile(rb"<title[^>]*>\s*(just a moment|attention required|bot verification"
                              rb"|access denied|checking your browser)", re.I)
_CHALLENGE_MAX_BYTES = 40_000
_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)


class BlockedURL(Exception):
    """Non-http(s) URL or one that resolves to a private/loopback/link-local address."""


class ResponseTooLarge(Exception):
    pass


class UnsupportedEncoding(Exception):
    pass


def _is_public_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _site_key(host: str) -> str:
    return host.lower().removeprefix("www.")


def block_kind(resp: httpx.Response) -> str | None:
    """'rate_limited', 'challenge' or 'forbidden' when the answer is the site refusing us."""
    if resp.status_code == 429:
        return "rate_limited"
    if resp.headers.get("cf-mitigated") or (
            len(resp.content) <= _CHALLENGE_MAX_BYTES and _CHALLENGE_TITLE.search(resp.content[:8000])):
        return "challenge"
    if resp.status_code == 403:
        return "forbidden"
    return None


def _page_title(body: bytes) -> str | None:
    m = _TITLE.search(body[:20_000])
    return m.group(1).decode("utf-8", "replace").strip()[:120] if m else None


def _blocker_headers(resp: httpx.Response) -> dict[str, str]:
    """Headers that say who answered and why, for the block log."""
    h = resp.headers
    out = {k.replace("-", "_"): h[k] for k in ("server", "cf-ray", "cf-mitigated", "cf-cache-status",
                                              "retry-after", "content-type", "x-cache", "via") if k in h}
    if "cf_ray" in out and "-" in out["cf_ray"]:
        out["cf_colo"] = out["cf_ray"].rsplit("-", 1)[1]  # Cloudflare data centre, e.g. DAC = Dhaka
    return out


def _retry_after(resp: httpx.Response) -> float | None:
    value = (resp.headers.get("retry-after") or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class HostCooldowns:
    """Sites that blocked us, left alone until a set time. Kept in a small JSON file."""

    def __init__(self, path: Path | None):
        self.path = path
        self.entries: dict[str, dict[str, str]] = {}

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("ignoring unreadable %s", self.path)
            return
        now = datetime.now(timezone.utc)
        for host, entry in (data.items() if isinstance(data, dict) else []):
            try:
                if datetime.fromisoformat(entry["until"]) > now:
                    self.entries[host] = entry
            except (KeyError, TypeError, ValueError):
                continue

    def until(self, host: str) -> str | None:
        entry = self.entries.get(host)
        if entry and datetime.fromisoformat(entry["until"]) > datetime.now(timezone.utc):
            return entry["until"]
        return None

    def add(self, host: str, reason: str, hours: float) -> None:
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        self.entries[host] = {"until": until.isoformat(timespec="seconds"), "reason": reason}

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


class Fetcher:
    def __init__(self, http_settings: dict, transport: httpx.AsyncBaseTransport | None = None, *,
                 sources: Iterable[Any] = (), cooldown_path: Path | None = None,
                 curl_session: Any = None, block_log: BlockLog | None = None):
        s = http_settings
        self._http_settings = dict(s)
        self.block_log = block_log
        self._sources = [(_site_key(urlsplit(src.homepage).hostname or ""), src) for src in sources]
        self.user_agent = s.get("user_agent", "AVNNewsScraper/0.1")
        self.robots_agent = s.get("robots_agent", "AVNNewsScraper")
        self.per_host_delay = float(s.get("per_host_delay", 1.0))
        self.jitter = float(s.get("per_host_jitter", 0.5))  # extra random delay, as a share of the delay
        self.retries = int(s.get("retries", 2))
        self.respect_robots = bool(s.get("respect_robots", True))
        self.max_bytes = int(float(s.get("max_response_mb", 20)) * 1024 * 1024)
        self.timeout = float(s.get("timeout_seconds", 25))
        self.block_threshold = int(s.get("block_threshold", 3))
        self.cooldown_hours = float(s.get("block_cooldown_hours", 12))
        self.max_retry_after = float(s.get("max_retry_after", 120))
        self.impersonate = str(s.get("impersonate", "chrome"))
        self.impersonate_delay = float(s.get("impersonate_delay", 3.0))
        self._impersonated = {_site_key(urlsplit(src.homepage).hostname or "")
                              for src in sources if getattr(src, "fetch", "plain") == "impersonate"}
        self._public_hosts: dict[str, bool] = {}
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": self.user_agent,
                "Accept-Language": "bn-BD,bn;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "gzip",  # the only coding _get_capped decodes
            },
            timeout=self.timeout,
            follow_redirects=True,
            event_hooks={"request": [self._check_target], "response": [self._check_peer]},
            transport=transport,
            trust_env=False,  # a proxy would hide the real peer address from _check_peer
        )
        self._curl = curl_session
        self._own_curl = False
        self._sem = asyncio.Semaphore(int(s.get("concurrency", 12)))
        self._host_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._host_last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._strikes: Counter[str] = Counter()  # block answers in a row, per host
        self._stopped: dict[str, str] = {}  # host -> why it is not asked again this run
        self.cooldowns = HostCooldowns(cooldown_path)
        self.stats: Counter[str] = Counter()
        self.failures: dict[str, str] = {}  # url -> reason, for the run log
        self._host_seq: Counter[str] = Counter()  # requests sent per host this run
        self._host_done: dict[str, float] = {}  # when the last request to the host finished

    async def __aenter__(self) -> Fetcher:
        await asyncio.to_thread(self.cooldowns.load)
        for host, entry in self.cooldowns.entries.items():
            log.warning("%s is on a block cooldown until %s (%s); not fetching it",
                        host, entry["until"], entry.get("reason"))
        if self._impersonated and self._curl is None:
            # trust_env=False: an environment proxy would hide the real peer address.
            self._curl = AsyncSession(impersonate=self.impersonate, timeout=self.timeout, trust_env=False)
            self._own_curl = True
        if self.block_log:
            await self.block_log.start(
                http_settings=self._http_settings, cooldowns=dict(self.cooldowns.entries),
                sources=[{"id": src.id, "host": key, "fetch": getattr(src, "fetch", "plain"),
                          "enabled": getattr(src, "enabled", True)} for key, src in self._sources])
        return self

    async def __aexit__(self, *exc) -> None:
        if self.block_log:
            await self.block_log.finish()
        await self._client.aclose()
        if self._own_curl:
            await self._curl.close()

    def source_for(self, host: str) -> str | None:
        key = _site_key(host)
        return next((src.id for site, src in self._sources if key == site or key.endswith("." + site)), None)

    def _tally(self, host: str, hostname: str, impersonate: bool):
        tally = self.block_log.hosts[host]
        if tally.source is None:
            tally.source = self.source_for(hostname)
            tally.client = self.impersonate if impersonate else "plain"
        return tally

    def uses_impersonation(self, host: str) -> bool:
        key = _site_key(host)
        return any(key == site or key.endswith("." + site) for site in self._impersonated)

    def stopped_hosts(self) -> dict[str, str]:
        """Hosts not fetched (this run's blocks and saved cooldowns), with the reason."""
        out = {h: f"cooldown until {e['until']} ({e.get('reason')})" for h, e in self.cooldowns.entries.items()}
        out.update(self._stopped)
        return out

    async def _ensure_public(self, url: str) -> None:
        """Early check by host name. Resolution failures block the request and are not cached.

        This check alone can be raced by DNS rebinding; the peer check is the authoritative one.
        """
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BlockedURL(url)
        host = parts.hostname
        if host not in self._public_hosts:
            try:
                ipaddress.ip_address(host)
                public = _is_public_ip(host)
            except ValueError:
                try:
                    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
                except OSError as exc:
                    raise BlockedURL(f"{url} (cannot resolve)") from exc
                public = bool(infos) and all(_is_public_ip(info[4][0]) for info in infos)
            self._public_hosts[host] = public
        if not self._public_hosts[host]:
            raise BlockedURL(url)

    async def _check_target(self, request: httpx.Request) -> None:
        """Request hook (first request and every redirect)."""
        await self._ensure_public(str(request.url))

    async def _check_peer(self, response: httpx.Response) -> None:
        """Response hook (every hop): the address actually connected to must be public.

        Runs before any body is read; httpx closes the response when this raises.
        """
        stream = response.extensions.get("network_stream")
        if stream is None:  # e.g. test transports
            return
        addr = stream.get_extra_info("server_addr")
        if addr and not _is_public_ip(addr[0]):
            raise BlockedURL(f"{response.request.url} (connected to {addr[0]})")

    async def _get_capped(self, url: str) -> httpx.Response:
        async with self._client.stream("GET", url) as r:
            coding = r.headers.get("content-encoding", "").strip().lower()
            if coding in ("", "identity"):
                decomp = None
            elif coding in ("gzip", "x-gzip"):
                decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
            else:  # stacked or unrequested codings ("gzip, gzip", "br", ...)
                raise UnsupportedEncoding(f"{url}: {coding}")
            body = bytearray()
            try:
                async for chunk in r.aiter_raw():
                    if decomp is None:
                        body += chunk
                    else:
                        # budget >= 1 here (0 would mean "unlimited" to zlib)
                        body += decomp.decompress(chunk, self.max_bytes + 1 - len(body))
                        if decomp.unconsumed_tail:
                            raise ResponseTooLarge(url)
                    if len(body) > self.max_bytes:
                        raise ResponseTooLarge(url)
                if decomp is not None:
                    body += decomp.flush()
            except zlib.error as exc:
                raise UnsupportedEncoding(f"{url}: corrupt gzip") from exc
            if len(body) > self.max_bytes:
                raise ResponseTooLarge(url)
            # The body is decoded now; drop the encoding headers to match.
            # Raw bytes: a header with non-ASCII text (a Bangla site name) can't be re-encoded as str.
            headers = [(k, v) for k, v in r.headers.raw if k.decode("latin-1").lower() not in _DROP_HEADERS]
            return httpx.Response(r.status_code, headers=headers, content=bytes(body), request=r.request)

    async def _get_impersonated(self, url: str) -> httpx.Response:
        """GET through curl_cffi with a browser TLS fingerprint and browser headers.

        Redirects are followed here, hop by hop, so each target gets the public-address
        check; curl decodes the body itself, in small chunks, which are counted
        against the size budget as they arrive.
        """
        parts = urlsplit(url)
        referer = f"{parts.scheme}://{parts.netloc}/"
        for _ in range(_MAX_REDIRECTS + 1):
            await self._ensure_public(url)
            headers = {"Accept-Language": "bn-BD,bn;q=0.9,en;q=0.8"}
            if url != referer:
                headers["Referer"] = referer
            async with self._curl.stream("GET", url, headers=headers, allow_redirects=False,
                                         timeout=self.timeout) as r:
                if r.primary_ip and not _is_public_ip(r.primary_ip):
                    raise BlockedURL(f"{url} (connected to {r.primary_ip})")
                location = r.headers.get("location")
                if 300 <= r.status_code < 400 and location:
                    url = urljoin(url, location)
                    continue
                body = bytearray()
                async for chunk in r.aiter_content():
                    body += chunk
                    if len(body) > self.max_bytes:
                        raise ResponseTooLarge(url)
                kept = [(k.encode("utf-8"), v.encode("utf-8")) for k, v in r.headers.multi_items()
                        if v is not None and k.lower() not in _DROP_HEADERS]
                return httpx.Response(r.status_code, headers=kept, content=bytes(body),
                                      request=httpx.Request("GET", url))
        raise httpx.TooManyRedirects(f"{url}: more than {_MAX_REDIRECTS} redirects")

    async def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_locks[origin]:
            if origin not in self._robots:
                parser: RobotFileParser | None = None
                resp = await self._request(f"{origin}/robots.txt", "robots")
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

    def _is_stopped(self, host: str, url: str) -> bool:
        stopped = "stopped_this_run" if host in self._stopped else "cooldown" if self.cooldowns.until(host) else None
        if stopped:
            self.stats["host_blocked"] += 1
            self.failures[url] = "host_blocked"
            if self.block_log:
                self.block_log.skip(host, stopped)
        return bool(stopped)

    async def _stop_host(self, host: str, reason: str) -> None:
        if host in self._stopped:
            return
        self._stopped[host] = reason
        self.cooldowns.add(host, reason, self.cooldown_hours)
        if self.block_log:
            self.block_log.host_stopped(host, reason, self.cooldowns.until(host), self._strikes[host])
        log.warning("%s keeps refusing us (%s): no more requests to it this run, and none for %g h "
                    "(delete it from %s to retry sooner)", host, reason, self.cooldown_hours, self.cooldowns.path)
        await asyncio.to_thread(self.cooldowns.save)

    async def _note_block(self, host: str, kind: str, url: str) -> None:
        self.stats[f"block_{kind}"] += 1
        self._strikes[host] += 1
        if self._strikes[host] >= self.block_threshold:
            await self._stop_host(host, f"{self._strikes[host]} block answers in a row, last: {kind} on {url}")

    async def _request(self, url: str, purpose: str = "page") -> httpx.Response | None:
        parts = urlsplit(url)
        host = parts.netloc
        impersonate = self.uses_impersonation(parts.hostname or "")
        delay = max(self.per_host_delay, self.impersonate_delay) if impersonate else self.per_host_delay
        if self.block_log:
            self._tally(host, parts.hostname or "", impersonate)
        resp: httpx.Response | None = None
        for attempt in range(self.retries + 1):
            error: str | None = None
            unsafe = False
            async with self._host_locks[host]:
                if self._is_stopped(host, url):
                    return None
                queued = time.monotonic()
                wait = self._host_last.get(host, 0) + delay * (1 + random.uniform(0, self.jitter)) - queued
                if wait > 0:
                    await asyncio.sleep(wait)
                sent = time.monotonic()
                self._host_seq[host] += 1
                try:
                    async with self._sem:
                        resp = await (self._get_impersonated(url) if impersonate else self._get_capped(url))
                except (BlockedURL, ResponseTooLarge, UnsupportedEncoding) as exc:
                    self.stats[type(exc).__name__] += 1
                    self.failures[url] = type(exc).__name__
                    log.warning("not fetching %s: %s", url, exc)
                    resp, unsafe, error = None, True, type(exc).__name__
                except (httpx.HTTPError, CurlError) as exc:
                    resp = None
                    error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    log.debug("fetch error %s: %r", url, exc)
                finally:
                    self._host_last[host] = time.monotonic()
                    previous = self._host_done.get(host)
                    self._host_done[host] = self._host_last[host]
            kind = block_kind(resp) if resp is not None else None
            if self.block_log:
                await self._log_attempt(host, url, purpose, attempt, resp, kind, error, unsafe,
                                        sent, queued, previous)
            if unsafe:
                return None
            if kind:
                await self._note_block(host, kind, url)
                if host in self._stopped:
                    return resp
                pause = _retry_after(resp) if kind == "rate_limited" else None
                if pause is not None and pause > self.max_retry_after:
                    await self._stop_host(host, f"asked to wait {pause:.0f} s")
                    return resp
                # Slow the whole site down, not just this URL: the pacing above makes
                # every later request to the host wait until then.
                if pause is None:
                    pause = 5.0 * 2 ** self._strikes[host]
                self._host_last[host] = time.monotonic() + pause
                if kind == "rate_limited" and attempt < self.retries:
                    continue
                return resp
            if resp is not None:
                self._strikes[host] = 0
                if resp.status_code < 500:
                    return resp
            if attempt < self.retries:
                await asyncio.sleep(2 * (attempt + 1) + random.uniform(0, 1))
        return resp

    async def _log_attempt(self, host: str, url: str, purpose: str, attempt: int, resp: httpx.Response | None,
                           kind: str | None, error: str | None, unsafe: bool, sent: float, queued: float,
                           previous: float | None) -> None:
        """One block-log entry per attempt (the log keeps lines only for failures)."""
        if unsafe:
            outcome = "unsafe"
        elif resp is None:
            outcome = "error"
        elif kind:
            outcome = "block"
        elif resp.status_code >= 500:
            outcome = "server_error"
        elif resp.status_code >= 400:
            outcome = "client_error"
        else:
            outcome = "ok"
        fields: dict[str, Any] = {
            "url": url, "purpose": purpose, "attempt": attempt + 1, "host_seq": self._host_seq[host],
            "gap_since_prev_s": round(sent - previous, 2) if previous is not None else None,
            "waited_s": round(sent - queued, 2),
            "elapsed_s": round(self._host_done[host] - sent, 2), "error": error,
        }
        if resp is not None:
            fields.update(status=resp.status_code, bytes=len(resp.content), headers=_blocker_headers(resp))
            if str(resp.url) != url:
                fields["final_url"] = str(resp.url)
        if kind:
            fields.update(block=kind, strikes=self._strikes[host] + 1, title=_page_title(resp.content),
                          sample=await self.block_log.save_sample(host, url, resp.status_code, resp.content))
        self.block_log.request(host, outcome, **fields)
        await self.block_log.maybe_flush()

    async def get(self, url: str, purpose: str = "page") -> httpx.Response | None:
        """GET url; None when blocked by robots.txt, on network error, block or non-2xx status.

        `purpose` (homepage, tab, feed, sitemap, article, api, search) only labels the block log.
        """
        if not await self.allowed(url):
            self.stats["robots_blocked"] += 1
            self.failures[url] = "robots_blocked"
            log.info("robots.txt disallows %s", url)
            if self.block_log:
                self.block_log.skip(urlsplit(url).netloc, "robots")
            return None
        resp = await self._request(url, purpose)
        if resp is None:
            if self.failures.get(url) != "host_blocked":
                self.stats["errors"] += 1
            self.failures.setdefault(url, "network_error")
            return None
        kind = block_kind(resp)
        if kind in ("challenge", "rate_limited"):
            # Temporary: the site refused us, not this page, so retry it on a later run.
            self.failures[url] = f"blocked_{kind}"
            log.debug("blocked (%s, HTTP %s) for %s", kind, resp.status_code, url)
            return None
        if resp.status_code >= 400:
            self.stats[f"http_{resp.status_code}"] += 1
            self.failures[url] = f"http_{resp.status_code}"
            log.debug("HTTP %s for %s", resp.status_code, url)
            return None
        self.stats["ok"] += 1
        return resp
