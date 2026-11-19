"""Permanent block log, kept to study how and when sites block us.

Successful requests are not logged one by one. Every scrape (`avn run`, dry run,
`check-sources`, a scheduled or API scrape) writes two files in a folder named after
the day of the scrape in Asia/Dhaka, with the start time and mode in the name:

    data/logs/blocks/2026-11-18/085935_run.jsonl           complete record, one event per line
    data/logs/blocks/2026-11-18/085935_run_events.parquet  one typed row per failure (to analyse)
    data/logs/blocks/2026-11-18/085935_run_hosts.parquet   one typed row per host (totals, block rate)
    data/logs/blocks/2026-11-18/085935_run/blocked/        the first block pages per host

This matches the run log `data/logs/runs/2026-11-18_085935.json`. Every line also carries
`run_id` (UTC start, e.g. 20261118T025935Z) and `scrape_date`.

JSONL events (`event` field):
- `run_start`: run id, scrape date, mode, news window, machine, git commit, library
  versions, http settings, the outlets with their fetch mode, cooldowns active at the start;
- `request`: a failed HTTP attempt only: a block (403, 429, challenge page), a timeout or
  connection error (an IP block often looks like one), or a 5xx, with its context (outlet,
  purpose, client, the nth request to the host, the gap since the previous one, how many
  succeeded before it, status, size, time taken, and the headers that name the blocker);
- `host_stopped`: the circuit breaker tripped, with the reason and the cooldown end;
- `run_end`: per-host summary (requests, ok, blocks by kind, statuses, skipped, the request
  number of the first block, stopped or not), so block rates can be computed.

Parquet is the format for bulk analysis: columnar, typed, compressed, and read directly
by DuckDB, pandas and Polars, across all runs at once, e.g.
`SELECT host, block, count(*) FROM 'data/logs/blocks/*/*_events.parquet' GROUP BY ALL`.
The events file holds the `request` and `host_stopped` events (EVENTS_SCHEMA), the hosts
file the run_end summary (HOSTS_SCHEMA). When the
scrape ends, finished files not yet in Postgres are imported (`sync_to_db`): the whole file
into `block_log_runs.raw`, the summary into `block_log_hosts`, the rows into
`block_log_events`. Files are never deleted, so a missed import is done next time.

Lines are buffered and written in a worker thread, so the event loop never waits on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import socket
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

_FLUSH_EVERY = 50
_SAMPLES_PER_HOST = 2
_SAMPLE_MAX_BYTES = 50_000
_STALE_SECONDS = 6 * 3600  # a file with no run_end this old is from a scrape that died
LOGGED_OUTCOMES = ("block", "error", "server_error")
HEADER_COLUMNS = ("server", "cf_ray", "cf_colo", "cf_mitigated", "retry_after", "content_type")
EVENT_COLUMNS = ("scrape_date", "run_id", "mode", "ts", "event", "host", "source", "client", "purpose",
                 "outcome", "block", "status", "attempt", "host_seq", "gap_since_prev_s", "waited_s",
                 "elapsed_s", "ok_before", "blocks_before", "strikes", "bytes", *HEADER_COLUMNS,
                 "title", "url", "final_url", "error", "sample", "reason", "until")


_TS = pa.timestamp("ms", tz="UTC")
EVENTS_SCHEMA = pa.schema([
    ("scrape_date", pa.date32()), ("run_id", pa.string()), ("mode", pa.string()), ("ts", _TS),
    ("event", pa.string()), ("host", pa.string()), ("source", pa.string()), ("client", pa.string()),
    ("purpose", pa.string()), ("outcome", pa.string()), ("block", pa.string()), ("status", pa.int16()),
    ("attempt", pa.int16()), ("host_seq", pa.int32()), ("gap_since_prev_s", pa.float32()),
    ("waited_s", pa.float32()), ("elapsed_s", pa.float32()), ("ok_before", pa.int32()),
    ("blocks_before", pa.int32()), ("strikes", pa.int16()), ("bytes", pa.int64()),
    *[(c, pa.string()) for c in HEADER_COLUMNS],
    ("title", pa.string()), ("url", pa.string()), ("final_url", pa.string()), ("error", pa.string()),
    ("sample", pa.string()), ("reason", pa.string()), ("until", _TS),
])
assert tuple(EVENTS_SCHEMA.names) == EVENT_COLUMNS
HOSTS_SCHEMA = pa.schema([
    ("scrape_date", pa.date32()), ("run_id", pa.string()), ("mode", pa.string()), ("host", pa.string()),
    ("source", pa.string()), ("client", pa.string()), ("requests", pa.int32()), ("ok", pa.int32()),
    ("blocked", pa.int32()), ("block_rate", pa.float32()), ("errors", pa.int32()), ("skipped", pa.int32()),
    ("challenge", pa.int32()), ("rate_limited", pa.int32()), ("forbidden", pa.int32()),
    ("first_block_seq", pa.int32()), ("stopped", pa.string()),
])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run_started(run_id: str) -> datetime:
    return datetime.strptime(run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def run_file(log_dir: Path, run_id: str, mode: str, tz: str) -> Path:
    """<log_dir>/<scrape date>/<HHMMSS>_<mode>.jsonl, both in local time `tz`."""
    local = run_started(run_id).astimezone(ZoneInfo(tz))
    return log_dir / f"{local:%Y-%m-%d}" / f"{local:%H%M%S}_{mode}.jsonl"


def flatten(event: dict[str, Any]) -> dict[str, Any]:
    """A request / host_stopped event as one flat row of EVENT_COLUMNS."""
    row = {k: event.get(k) for k in EVENT_COLUMNS}
    for k in HEADER_COLUMNS:
        row[k] = (event.get("headers") or {}).get(k)
    return row


def host_rows(run_id: str, scrape_date: str, mode: str, summary: dict[str, dict]) -> list[dict[str, Any]]:
    """The run_end per-host summary as flat rows (HOSTS_SCHEMA; the database uses them too)."""
    rows = []
    for host, t in summary.items():
        blocks = t.get("blocks") or {}
        blocked = sum(blocks.values())
        rows.append({"scrape_date": scrape_date, "run_id": run_id, "mode": mode, "host": host,
                     "source": t.get("source"), "client": t.get("client"), "requests": t.get("requests", 0),
                     "ok": t.get("ok", 0), "blocked": blocked,
                     "block_rate": round(blocked / t["requests"], 4) if t.get("requests") else None,
                     "errors": t.get("errors", 0), "skipped": sum((t.get("skipped") or {}).values()),
                     "challenge": blocks.get("challenge", 0), "rate_limited": blocks.get("rate_limited", 0),
                     "forbidden": blocks.get("forbidden", 0), "first_block_seq": t.get("first_block_seq"),
                     "stopped": t.get("stopped"), "blocks": blocks, "statuses": t.get("statuses") or {}})
    return rows


def _typed(value: Any, kind: pa.DataType) -> Any:
    if value is None or value == "":
        return None
    if kind == _TS:
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if kind == pa.date32():
        return date.fromisoformat(value) if isinstance(value, str) else value
    if pa.types.is_integer(kind):
        return int(value)
    if pa.types.is_floating(kind):
        return float(value)
    return str(value)


def write_parquet(path: Path, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    """Rows as a typed, zstd-compressed Parquet file (written even with no rows, for its schema)."""
    columns = {f.name: [_typed(r.get(f.name), f.type) for r in rows] for f in schema}
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(columns, schema=schema), path, compression="zstd")


def _version(pkg: str) -> str | None:
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return None


async def _git_commit(cwd: Path) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(cwd), "rev-parse", "--short", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return None
    return (out.decode().strip() or None) if proc.returncode == 0 else None


class HostTally:
    """Running per-host numbers: context for each block line and the run_end summary."""

    def __init__(self) -> None:
        self.source: str | None = None
        self.client = "plain"
        self.requests = 0
        self.ok = 0
        self.errors = 0
        self.skipped: Counter[str] = Counter()
        self.statuses: Counter[str] = Counter()
        self.blocks: Counter[str] = Counter()
        self.first_block_seq: int | None = None
        self.first_block_at: str | None = None
        self.stopped: str | None = None
        self.samples = 0

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "client": self.client, "requests": self.requests, "ok": self.ok,
                "errors": self.errors, "skipped": dict(self.skipped), "statuses": dict(self.statuses),
                "blocks": dict(self.blocks), "first_block_seq": self.first_block_seq,
                "first_block_at": self.first_block_at, "stopped": self.stopped}


class BlockLog:
    def __init__(self, path: Path, run_id: str, mode: str, scrape_date: str):
        self.path = path
        self.run_id = run_id
        self.mode = mode
        self.scrape_date = scrape_date
        self.started = datetime.now(timezone.utc)
        self.hosts: defaultdict[str, HostTally] = defaultdict(HostTally)
        self.rows: list[dict[str, Any]] = []  # flat failure rows, for the events Parquet
        self.window: tuple[datetime, datetime] | None = None  # news window, set by the pipeline
        self._buffer: list[str] = []
        self._lock = asyncio.Lock()

    @classmethod
    def for_run(cls, log_dir: Path, run_id: str, mode: str, tz: str = "Asia/Dhaka") -> BlockLog:
        path = run_file(log_dir, run_id, mode, tz)
        return cls(path, run_id, mode, path.parent.name)

    @property
    def events_path(self) -> Path:
        return self.path.with_name(self.path.stem + "_events.parquet")

    @property
    def hosts_path(self) -> Path:
        return self.path.with_name(self.path.stem + "_hosts.parquet")

    @property
    def sample_dir(self) -> Path:
        return self.path.with_suffix("") / "blocked"

    def event(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": now_iso(), "scrape_date": self.scrape_date, "run_id": self.run_id, "mode": self.mode,
                  "event": event, **fields}
        self._buffer.append(json.dumps(record, ensure_ascii=False, default=str))
        return record

    async def start(self, *, http_settings: dict, sources: list[dict], cooldowns: dict) -> None:
        window = self.window
        self.event("run_start", started_at=self.started.isoformat(timespec="seconds"),
                   window_from=window[0].isoformat(timespec="seconds") if window else None,
                   window_to=window[1].isoformat(timespec="seconds") if window else None,
                   machine=socket.gethostname(), git_commit=await _git_commit(Path(__file__).resolve().parent),
                   versions={"python": platform.python_version(), "avn": _version("avn-news-scraper"),
                             "httpx": _version("httpx"), "curl_cffi": _version("curl_cffi")},
                   http_settings=http_settings, sources=sources, cooldowns_at_start=cooldowns)
        await self.flush()

    def request(self, host: str, outcome: str, **fields: Any) -> None:
        """Count every attempt; write a line only for failed ones (LOGGED_OUTCOMES)."""
        t = self.hosts[host]
        t.requests += 1
        if fields.get("status") is not None:
            t.statuses[str(fields["status"])] += 1
        if outcome == "ok":
            t.ok += 1
        elif outcome == "block":
            t.blocks[fields.get("block") or "?"] += 1
            if t.first_block_seq is None:
                t.first_block_seq = fields.get("host_seq")
                t.first_block_at = now_iso()
        elif outcome in ("error", "server_error"):
            t.errors += 1
        if outcome in LOGGED_OUTCOMES:
            record = self.event("request", host=host, source=t.source, client=t.client, outcome=outcome,
                                ok_before=t.ok, blocks_before=sum(t.blocks.values()) - (outcome == "block"),
                                **fields)
            self.rows.append(flatten(record))

    def skip(self, host: str, reason: str) -> None:
        """A URL not requested (host stopped, cooldown, robots.txt): counted, not logged per URL."""
        self.hosts[host].skipped[reason] += 1

    def host_stopped(self, host: str, reason: str, until: str | None, strikes: int) -> None:
        t = self.hosts[host]
        t.stopped = reason
        record = self.event("host_stopped", host=host, source=t.source, client=t.client, reason=reason,
                            until=until, strikes=strikes, requests_so_far=t.requests, ok_so_far=t.ok)
        self.rows.append(flatten(record))

    async def save_sample(self, host: str, url: str, status: int, body: bytes) -> str | None:
        """Keep the first few block pages per host; returns the saved file name."""
        t = self.hosts[host]
        if t.samples >= _SAMPLES_PER_HOST:
            return None
        t.samples += 1
        name = f"{host.replace(':', '_')}_{t.samples}_{status}.html"
        path = self.sample_dir / name
        header = f"<!-- {url} | HTTP {status} | {now_iso()} -->\n".encode()

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(header + body[:_SAMPLE_MAX_BYTES])
        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            log.warning("could not save block sample %s: %s", path, exc)
            return None
        return name

    async def maybe_flush(self) -> None:
        if len(self._buffer) >= _FLUSH_EVERY:
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            lines, self._buffer = self._buffer, []

            def write() -> None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            try:
                await asyncio.to_thread(write)
            except OSError as exc:  # never fail a scrape over its log
                log.warning("could not write block log %s: %s", self.path, exc)

    def summary(self) -> dict[str, dict[str, Any]]:
        return {host: t.as_dict() for host, t in sorted(self.hosts.items())}

    async def finish(self) -> None:
        finished = datetime.now(timezone.utc)
        totals: Counter[str] = Counter()
        for t in self.hosts.values():
            totals["requests"] += t.requests
            totals["ok"] += t.ok
            totals["blocks"] += sum(t.blocks.values())
            totals["errors"] += t.errors
            totals["skipped"] += sum(t.skipped.values())
            totals["hosts_stopped"] += bool(t.stopped)
        self.event("run_end", finished_at=finished.isoformat(timespec="seconds"),
                   duration_seconds=round((finished - self.started).total_seconds(), 1),
                   totals=dict(totals), hosts=self.summary())
        await self.flush()
        await asyncio.to_thread(self._write_parquet)

    def _write_parquet(self) -> None:
        hosts = host_rows(self.run_id, self.scrape_date, self.mode, self.summary())
        for path, rows, schema in ((self.events_path, self.rows, EVENTS_SCHEMA),
                                   (self.hosts_path, hosts, HOSTS_SCHEMA)):
            try:
                write_parquet(path, rows, schema)
            except (OSError, pa.ArrowException, ValueError) as exc:  # never fail a scrape over its log
                log.warning("could not write %s: %s", path, exc)


# --- reading back --------------------------------------------------------------------------

def log_files(log_dir: Path, since: date | None = None) -> list[Path]:
    """Run files, oldest first, from scrape-date folders on or after `since`."""
    out = []
    for folder in sorted(p for p in log_dir.glob("????-??-??") if p.is_dir()):
        if since is None or folder.name >= since.isoformat():
            out += sorted(folder.glob("*.jsonl"))
    return out


def _read_lines(path: Path) -> list[dict[str, Any]]:
    events = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def read_events(log_dir: Path, since: date | None = None) -> list[dict[str, Any]]:
    """All events from the run files, oldest run first. Skips broken lines."""
    return [e for path in log_files(log_dir, since) for e in _read_lines(path)]


def parse_file(path: Path, log_dir: Path) -> dict[str, Any] | None:
    """A run file ready for the database, or None while its scrape is still writing it."""
    events = _read_lines(path)
    start = next((e for e in events if e.get("event") == "run_start"), None)
    end = next((e for e in events if e.get("event") == "run_end"), None)
    if start is None:
        return None
    if end is None and time.time() - path.stat().st_mtime < _STALE_SECONDS:
        return None  # still running (or just died; imported once it is stale)
    run = {"run_id": start["run_id"], "mode": start["mode"],
           "scrape_date": start.get("scrape_date") or path.parent.name,
           "started_at": start.get("started_at") or start["ts"],
           "finished_at": (end or {}).get("finished_at"),
           "window_from": start.get("window_from"), "window_to": start.get("window_to"),
           "machine": start.get("machine"), "git_commit": start.get("git_commit"),
           "file_name": str(path.relative_to(log_dir)), "totals": (end or {}).get("totals")}
    hosts = host_rows(run["run_id"], run["scrape_date"], run["mode"], (end or {}).get("hosts") or {})
    rows = [flatten(e) | {"scrape_date": run["scrape_date"]}
            for e in events if e.get("event") in ("request", "host_stopped")]
    return {"run": run, "hosts": hosts, "events": rows, "raw": events}


async def sync_to_db(storage: Any, log_dir: Path) -> list[str]:
    """Import every finished run file not yet in Postgres. Returns the imported run ids.

    Never raises: the files stay the source of truth, and a failed import is retried
    by the next scrape (or `avn sync-block-logs`).
    """
    try:
        known = await storage.known_block_logs()
        files = await asyncio.to_thread(log_files, log_dir)
        imported = []
        for path in files:
            parsed = await asyncio.to_thread(parse_file, path, log_dir)
            if parsed is None or parsed["run"]["run_id"] in known:
                continue
            await storage.save_block_log(parsed)
            known.add(parsed["run"]["run_id"])
            imported.append(parsed["run"]["run_id"])
        return imported
    except Exception as exc:  # noqa: BLE001 - logging must never fail a scrape
        log.warning("block logs not imported into Postgres this time: %r", exc)
        return []
