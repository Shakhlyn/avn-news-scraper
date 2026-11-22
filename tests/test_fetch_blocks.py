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


# --- block log -----------------------------------------------------------------------------

def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_block_log_keeps_failures_not_successes(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    from avn_scraper.scraper.blocklog import BlockLog
    blog = BlockLog.for_run(tmp_path, "20261118T030000Z", "dry-run")
    answers = iter([200, 200, 403, 200, 404])

    def handler(request):
        status = next(answers)
        if status == 403:
            return httpx.Response(403, headers={"cf-mitigated": "challenge", "cf-ray": "8abc-DAC", "server": "cloudflare"},
                                  content=CHALLENGE)
        return httpx.Response(status, content=b"<html>page</html>")

    src = SimpleNamespace(id="paper", homepage="https://93.184.216.34/", fetch="plain")

    async def go(f):
        for i in range(5):
            await f.get(f"https://93.184.216.34/{i}", "article")
    _run(go, handler, sources=[src], block_log=blog)

    path = tmp_path / "2026-11-18" / "090000_dry-run.jsonl"  # 03:00 UTC = 09:00 in Dhaka
    assert blog.path == path
    events = _events(path)
    assert [e["event"] for e in events] == ["run_start", "request", "run_end"]
    block = events[1]
    assert block["outcome"] == "block" and block["block"] == "challenge"
    assert block["source"] == "paper" and block["purpose"] == "article"
    assert block["host_seq"] == 3 and block["ok_before"] == 2 and block["blocks_before"] == 0
    assert block["headers"]["cf_colo"] == "DAC" and block["title"] == "Just a moment..."
    assert (path.with_suffix("") / "blocked" / block["sample"]).exists()
    host = events[2]["hosts"]["93.184.216.34"]
    assert host["requests"] == 5 and host["ok"] == 3 and host["blocks"] == {"challenge": 1}
    assert host["first_block_seq"] == 3 and host["statuses"]["404"] == 1
    assert events[0]["sources"][0]["id"] == "paper"
    assert all(e["scrape_date"] == "2026-11-18" for e in events)
    import datetime as dt

    import pyarrow.parquet as pq
    table = pq.read_table(blog.events_path)
    rows = table.to_pylist()
    assert len(rows) == 1 and rows[0]["block"] == "challenge" and rows[0]["cf_colo"] == "DAC"
    assert rows[0]["scrape_date"] == dt.date(2026, 11, 18) and rows[0]["purpose"] == "article"
    assert rows[0]["status"] == 403 and rows[0]["host_seq"] == 3 and rows[0]["ts"].tzinfo is not None
    hosts = pq.read_table(blog.hosts_path).to_pylist()
    assert hosts[0]["requests"] == 5 and hosts[0]["challenge"] == 1 and abs(hosts[0]["block_rate"] - 0.2) < 1e-6


def test_block_log_records_stop_and_skips(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    from avn_scraper.scraper.blocklog import BlockLog
    blog = BlockLog.for_run(tmp_path, "20261118T030000Z", "run")

    async def go(f):
        for i in range(5):
            await f.get(f"https://93.184.216.34/{i}")
    _run(go, lambda r: httpx.Response(429), block_log=blog, cooldown_path=tmp_path / "b.json")
    events = _events(blog.path)
    kinds = [e["event"] for e in events]
    assert kinds.count("request") == 3 and kinds.count("host_stopped") == 1
    stop = next(e for e in events if e["event"] == "host_stopped")
    assert stop["strikes"] == 3 and stop["until"]
    end = events[-1]
    assert end["event"] == "run_end" and end["hosts"]["93.184.216.34"]["skipped"] == {"stopped_this_run": 2}
    assert end["totals"]["hosts_stopped"] == 1


def test_block_log_records_network_errors(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    from avn_scraper.scraper.blocklog import BlockLog
    blog = BlockLog.for_run(tmp_path, "20261118T030000Z", "run")

    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    _run(lambda f: f.get("https://93.184.216.34/x"), handler, block_log=blog)
    req = [e for e in _events(blog.path) if e["event"] == "request"]
    assert len(req) == 1 and req[0]["outcome"] == "error" and req[0]["error"].startswith("ConnectTimeout")


def test_dead_host_stopped_after_repeated_timeouts_without_cooldown(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    from avn_scraper.scraper.blocklog import BlockLog
    blog = BlockLog.for_run(tmp_path, "20261118T030000Z", "run")
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "93.184.216.34":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, content=b"<html>ok</html>")

    async def go(f):
        dead = [await f.get(f"https://93.184.216.34/{i}") for i in range(10)]
        return dead, await f.get("https://1.1.1.1/other"), f
    dead, other, f = _run(go, handler, settings={"retries": 1, "error_threshold": 5},
                          cooldown_path=tmp_path / "blocks.json", block_log=blog)
    assert dead == [None] * 10 and other is not None
    assert calls.count("93.184.216.34") == 5  # 2 pages x 2 tries, then 1 try of the 3rd page
    assert f.failures["https://93.184.216.34/9"] == "host_blocked"
    assert "ReadTimeout" in f.stopped_hosts()["93.184.216.34"]
    assert not (tmp_path / "blocks.json").exists() or "93.184.216.34" not in (tmp_path / "blocks.json").read_text()
    stop = next(e for e in _events(blog.path) if e["event"] == "host_stopped")
    assert stop["host"] == "93.184.216.34" and stop["until"] is None


def test_an_answer_resets_the_network_error_count(monkeypatch):
    _no_sleep(monkeypatch)
    answers = iter(["timeout"] * 4 + ["ok"] + ["timeout"] * 4 + ["ok"])

    def handler(request):
        if next(answers) == "timeout":
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, content=b"<html>ok</html>")

    async def go(f):
        return [await f.get(f"https://93.184.216.34/{i}") for i in range(10)], f.stopped_hosts()
    out, stopped = _run(go, handler, settings={"error_threshold": 5})
    assert out[4] is not None and out[9] is not None
    assert stopped == {}


def test_block_report_reads_the_logs(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    import shutil
    from pathlib import Path

    from typer.testing import CliRunner

    from avn_scraper.cli import app
    from avn_scraper.scraper.blocklog import BlockLog
    from avn_scraper.scraper.config import DEFAULT_CONFIG_DIR

    cfg_dir = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, cfg_dir)
    settings = (cfg_dir / "settings.yaml").read_text().replace("data_dir: data", f"data_dir: {tmp_path / 'data'}")
    (cfg_dir / "settings.yaml").write_text(settings)
    from datetime import datetime, timezone
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    blog = BlockLog.for_run(tmp_path / "data" / "logs" / "blocks", run_id, "run")
    _run(lambda f: f.get("https://93.184.216.34/x", "tab"),
         lambda r: httpx.Response(403, headers={"cf-mitigated": "challenge"}, content=CHALLENGE), block_log=blog)
    monkeypatch.setenv("COLUMNS", "220")
    result = CliRunner().invoke(app, ["block-report", "--config", str(cfg_dir)])
    assert result.exit_code == 0, result.output
    assert "93.184.216.34" in result.output and "challenge" in result.output and "tab" in result.output
    assert Path(blog.path).exists()


def test_scrape_date_is_the_dhaka_day(tmp_path):
    from avn_scraper.scraper.blocklog import BlockLog
    late = BlockLog.for_run(tmp_path, "20261117T200000Z", "run")  # 02:00 on the 18th in Dhaka
    assert late.path == tmp_path / "2026-11-18" / "020000_run.jsonl" and late.scrape_date == "2026-11-18"


def _write_finished_log(tmp_path, monkeypatch, run_id="20261118T030000Z"):
    from avn_scraper.scraper.blocklog import BlockLog
    blog = BlockLog.for_run(tmp_path, run_id, "run")

    async def go(f):
        for i in range(4):
            await f.get(f"https://93.184.216.34/{i}", "tab")
    _run(go, lambda r: httpx.Response(403, headers={"cf-mitigated": "challenge"}, content=CHALLENGE),
         block_log=blog, cooldown_path=tmp_path / "b.json")
    return blog


def test_parse_file_waits_for_a_running_scrape(tmp_path):
    from avn_scraper.scraper.blocklog import parse_file
    path = tmp_path / "2026-11-18" / "090000_run.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"event": "run_start", "run_id": "20261118T030000Z", "mode": "run",
                                "ts": "2026-11-18T03:00:00+00:00"}) + "\n")
    assert parse_file(path, tmp_path) is None  # no run_end yet and fresh


def test_block_logs_imported_into_postgres_once(tmp_path, monkeypatch, database):
    _no_sleep(monkeypatch)
    from avn_scraper.scraper.blocklog import sync_to_db
    from avn_scraper.scraper.storage import PostgresStorage
    blog = _write_finished_log(tmp_path, monkeypatch)

    async def go():
        storage = await PostgresStorage.open(tmp_path)
        try:
            first = await sync_to_db(storage, tmp_path)
            second = await sync_to_db(storage, tmp_path)
            run = (await storage.query_df("SELECT * FROM block_log_runs")).iloc[0]
            hosts = await storage.query_df("SELECT * FROM block_log_hosts")
            events = await storage.query_df("SELECT * FROM block_log_events ORDER BY id")
            return first, second, run, hosts, events
        finally:
            await storage.close()
    first, second, run, hosts, events = asyncio.run(go())
    assert first == ["20261118T030000Z"] and second == []
    assert str(run["scrape_date"]) == "2026-11-18" and run["file_name"] == "2026-11-18/090000_run.jsonl"
    assert run["raw"][0]["event"] == "run_start" and run["raw"][-1]["event"] == "run_end"
    assert hosts.iloc[0]["blocked"] == 3 and hosts.iloc[0]["stopped"]
    assert list(events["event"]) == ["request", "request", "request", "host_stopped"]
    assert events.iloc[0]["purpose"] == "tab" and events.iloc[0]["status"] == 403
    import pyarrow.parquet as pq
    assert len(events) == pq.read_metadata(blog.events_path).num_rows


def test_parquet_files_read_together_across_runs(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    import pyarrow.dataset as ds
    _write_finished_log(tmp_path, monkeypatch, "20261117T030000Z")
    _write_finished_log(tmp_path / "b", monkeypatch, "20261118T030000Z")  # separate cooldown file
    for p in (tmp_path / "b").glob("2026-11-18"):
        p.rename(tmp_path / "2026-11-18")
    table = ds.dataset(sorted(map(str, tmp_path.glob("*/*_events.parquet")))).to_table()
    assert table.num_rows == 8 and set(table.column("scrape_date").to_pylist()) == {
        __import__("datetime").date(2026, 11, 17), __import__("datetime").date(2026, 11, 18)}


def test_run_without_failures_still_writes_typed_files(tmp_path, monkeypatch):
    _no_sleep(monkeypatch)
    import pyarrow.parquet as pq
    from avn_scraper.scraper.blocklog import EVENTS_SCHEMA, BlockLog
    blog = BlockLog.for_run(tmp_path, "20261118T030000Z", "check-sources")
    _run(lambda f: f.get("https://93.184.216.34/x"), lambda r: httpx.Response(200, content=b"<html>ok</html>"),
         block_log=blog)
    events = pq.read_table(blog.events_path)
    assert events.num_rows == 0 and events.schema.equals(EVENTS_SCHEMA)
    assert pq.read_table(blog.hosts_path).to_pylist()[0]["ok"] == 1


# --- hard deadline per request -------------------------------------------------------------

class _StuckCurlResponse(_FakeCurlResponse):
    """A stream that never ends and, like a curl_cffi stream on a dead connection, only
    closes once quit_now is set; a cancel alone does not stop it."""

    def __init__(self):
        super().__init__(200)
        self.quit_now = asyncio.Event()

    async def aiter_content(self):
        yield b"<html>"
        while not self.quit_now.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue  # ignores cancellation


def test_request_past_deadline_is_a_timeout(monkeypatch):
    _public_dns(monkeypatch)
    monkeypatch.setattr(fetch, "_CANCEL_GRACE_SECONDS", 0.05)
    url = "https://www.example-paper.com/slow"
    stuck = _StuckCurlResponse()
    sessions = []

    class Session(_FakeCurl):
        def __init__(self, **kwargs):
            super().__init__({url: stuck})
            self.closed = False
            sessions.append(self)

        async def close(self):  # closing the session ends its transfers
            self.closed = True
            stuck.quit_now.set()

    monkeypatch.setattr(fetch, "AsyncSession", Session)
    src = SimpleNamespace(homepage="https://www.example-paper.com/", fetch="impersonate")

    async def go(f):
        return await f.get(url), f.failures
    resp, failures = _run(go, settings={"request_deadline_seconds": 0.1}, sources=[src])
    assert resp is None and failures[url] == "network_error"
    assert len(sessions) == 2 and sessions[0].closed  # the wedged session was replaced


def test_trickling_plain_response_cut_at_deadline():
    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:  # a byte now and then: each read is well inside timeout_seconds
                yield b"a"
                await asyncio.sleep(0.01)

    async def go():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Trickle()))
        async with Fetcher({**SETTINGS, "request_deadline_seconds": 0.2}, transport=transport) as f:
            return await f.get("https://93.184.216.34/slow"), f.failures
    resp, failures = asyncio.run(go())
    assert resp is None and failures["https://93.184.216.34/slow"] == "network_error"


def test_leaving_a_curl_stream_early_tells_curl_to_stop(monkeypatch):
    _public_dns(monkeypatch)
    url = "https://www.example-paper.com/r"
    first = _FakeCurlResponse(302, headers={"location": "https://www.example-paper.com/final"})
    first.quit_now = asyncio.Event()
    routes = {url: first, "https://www.example-paper.com/final": _FakeCurlResponse(200, b"<html>ok</html>")}
    (resp, _) = _impersonated(routes, lambda f: f.get(url))
    assert resp.text == "<html>ok</html>" and first.quit_now.is_set()
