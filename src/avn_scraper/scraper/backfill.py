"""Backfill: scrape past months on demand (`avn backfill 2025-05`), next to the daily run.

Old articles come from each outlet's archive sitemaps (discover/archive.py). Each month is
one run: discover -> skip known -> keyword pre-filter -> download -> classify -> store,
with the same rules as the daily run, saved article by article. Progress is kept per
outlet and month in `backfill_progress`, so an interrupted backfill resumes. While the
daily scrape (or `avn run`) holds the run lock, the backfill waits.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .blocklog import BlockLog, sync_to_db
from .classify import Classifier
from .config import Config, Source
from .discover import Candidate
from .discover.archive import ArchiveLayout, ArchiveReader, ArchiveResult, discover_layout, headline_text, month_window
from .export import export_all
from .fetch import Fetcher
from .lock import is_locked
from .pipeline import _PERMANENT_FAILURES, Judge, _fetch_one, _save, _write_run_log
from .storage import PostgresStorage, Storage
from .urls import article_id, host_of, slug_text

log = logging.getLogger(__name__)

_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_YEAR = re.compile(r"^(\d{4})$")
UNTITLED = ("download", "skip")
# Sections with no article text; untitled links in them are not downloaded.
_MEDIA = re.compile(r"/(videos?|photos?|gallery|galleries|tube|podcasts?|live|horoscope|photo-?story)(/|$)", re.I)
FINISHED = ("done", "no_archive")  # statuses a later backfill does not redo


def _month(spec: str) -> date:
    m = _MONTH.match(spec)
    if not m or not 1 <= int(m[2]) <= 12:
        raise ValueError(f"not a month: {spec!r} (use YYYY-MM)")
    return date(int(m[1]), int(m[2]), 1)


def _months_between(first: date, last: date) -> list[date]:
    out, d = [], first
    while d <= last:
        out.append(d)
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return out


def parse_months(specs: list[str], today: date) -> list[date]:
    """'2025-05', '2025-05 2025-07', '2025-01..2025-06', '2025' -> first days of months,
    sorted. A year means its months up to now. The current month runs up to yesterday;
    future months are refused."""
    this_month = today.replace(day=1)
    months: set[date] = set()
    for spec in specs:
        for part in (p.strip() for p in spec.split(",") if p.strip()):
            if ".." in part:
                a, b = (x.strip() for x in part.split("..", 1))
                first, last = _month(a), _month(b)
                if last < first:
                    raise ValueError(f"range goes backwards: {part!r}")
                months.update(_months_between(first, last))
            elif m := _YEAR.match(part):
                months.update(d for i in range(1, 13) if (d := date(int(m[1]), i, 1)) <= this_month)
            else:
                months.add(_month(part))
    future = sorted(m for m in months if m > this_month)
    if future:
        raise ValueError(f"months in the future: {', '.join(f'{m:%Y-%m}' for m in future)}")
    if today.day == 1:
        months.discard(this_month)  # nothing of it is over yet
    if not months:
        raise ValueError("no past month given, e.g. 2025-05")
    return sorted(months)


def _select(candidates: dict[str, Candidate], known: set[str], classifier: Classifier, untitled: str,
            per: dict[str, Counter]) -> list[Candidate]:
    """New candidates whose headline (sitemap title or URL slug) has a keyword. Those with
    no headline at all are downloaded when untitled == "download"."""
    selected: list[Candidate] = []
    for key, c in candidates.items():
        if key in known:
            per[c.source_id]["known"] += 1
        elif not headline_text(c):
            per[c.source_id]["untitled"] += 1
            if _MEDIA.search(urlsplit(c.url).path):
                per[c.source_id]["untitled_media"] += 1  # videos, photos: no text to match
            elif untitled == "download":
                selected.append(c)
        elif classifier.prefilter(c.title, c.summary, slug_text(c.url)):
            selected.append(c)
    for c in selected:
        per[c.source_id]["selected"] += 1
    return selected


@dataclass
class BackfillOptions:
    months: list[date]
    only_sources: list[str] | None = None
    dry_run: bool = False
    redo: bool = False  # also redo outlet-months already done
    untitled: str | None = None  # None = settings.yaml backfill.untitled


@dataclass
class MonthResult:
    month: date
    run_id: str | None = None
    skipped: bool = False  # every outlet already done
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcome: Counter[str] = field(default_factory=Counter)
    saved: list[dict[str, Any]] = field(default_factory=list)
    stopped: dict[str, str] = field(default_factory=dict)
    duration: float = 0.0


class RunGate:
    """Holds the backfill back while the daily scrape or `avn run` holds the run lock,
    so the two never hit the same sites at once."""

    def __init__(self, lock_path: Path, poll_seconds: float = 30.0):
        self.lock_path = lock_path
        self.poll = poll_seconds
        self._checked = 0.0
        self._pause: asyncio.Lock | None = None

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        if self._pause is None:
            self._pause = asyncio.Lock()
        async with self._pause:  # one task polls, the rest queue behind it
            if loop.time() - self._checked < 5:
                return
            told = False
            while await asyncio.to_thread(is_locked, self.lock_path):
                if not told:
                    log.info("a scrape is running; backfill paused until it ends")
                    told = True
                await asyncio.sleep(self.poll)
            if told:
                log.info("scrape finished; backfill continues")
            self._checked = loop.time()


def _http_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """The daily run's HTTP settings, with the backfill's slower per-site pace."""
    http = dict(settings.get("http") or {})
    bf = settings.get("backfill") or {}
    http["per_host_delay"] = max(float(http.get("per_host_delay", 1.0)), float(bf.get("per_host_delay", 2.0)))
    return http


async def run_backfill(cfg: Config, opts: BackfillOptions, transport: httpx.AsyncBaseTransport | None = None,
                       gate: RunGate | None = None,
                       on_month: Callable[[MonthResult], None] | None = None) -> list[MonthResult]:
    untitled = opts.untitled or (cfg.settings.get("backfill") or {}).get("untitled", "download")
    if untitled not in UNTITLED:
        raise ValueError(f"untitled must be one of {UNTITLED}")
    storage = await PostgresStorage.open(cfg.data_dir)
    gate = gate or RunGate(cfg.data_dir / ".run.lock",
                           float((cfg.settings.get("backfill") or {}).get("pause_poll_seconds", 30)))
    classifier = await asyncio.to_thread(Classifier, cfg.keywords)
    layouts: dict[str, ArchiveLayout] = {}
    reader = ArchiveReader()
    results: list[MonthResult] = []
    try:
        if not opts.dry_run:
            await storage.upsert_sources(cfg.sources)
        for month in opts.months:
            result = await _backfill_month(cfg, opts, storage, month, untitled, classifier, layouts, reader,
                                           transport, gate)
            results.append(result)
            if on_month:
                on_month(result)
        if not opts.dry_run and any(r.outcome.get("saved") for r in results):
            await export_all(storage, cfg.data_dir / "exports", classifier.labels, cfg.timezone)
    finally:
        await storage.close()
    return results


async def _backfill_month(cfg: Config, opts: BackfillOptions, storage: Storage, month: date, untitled: str,
                          classifier: Classifier, layouts: dict[str, ArchiveLayout], reader: ArchiveReader,
                          transport: httpx.AsyncBaseTransport | None, gate: RunGate) -> MonthResult:
    tz = ZoneInfo(cfg.timezone)
    today = datetime.now(tz).date()
    start, end = month_window(month, tz, today)
    result = MonthResult(month)
    finished_month = end.date() > date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
    done = set() if opts.redo else {r["source_id"] for r in await storage.backfill_progress([month])
                                    if r["status"] in FINISHED}
    sources = [s for s in cfg.sources if s.enabled and (not opts.only_sources or s.id in opts.only_sources)
               and s.id not in done]
    if not sources or end <= start:
        result.skipped = True
        return result

    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    result.run_id = run_id
    by_id = {s.id: s for s in cfg.sources}
    block_log = BlockLog.for_run(cfg.block_log_dir, run_id, "dry-backfill" if opts.dry_run else "backfill",
                                 cfg.timezone)
    block_log.window = (start.astimezone(timezone.utc), end.astimezone(timezone.utc))
    per: dict[str, Counter] = {s.id: Counter() for s in sources}
    info: dict[str, dict[str, Any]] = {}

    async with Fetcher(_http_settings(cfg.settings), transport=transport, sources=cfg.sources,
                       cooldown_path=cfg.host_blocks_path, block_log=block_log) as fetcher:
        reader.use(fetcher)

        async def discover(src: Source) -> tuple[Source, ArchiveResult]:
            await gate.wait()
            try:
                layout = layouts.get(src.id)
                if layout is None:
                    layout = await discover_layout(fetcher, src)
                    if layout.indexes:  # only remember what the site actually answered
                        layouts[src.id] = layout
                info[src.id] = {"layout": layout.kind, "indexes": layout.indexes}
                if not layout.indexes:
                    return src, ArchiveResult(src.id, error="sitemap index not reachable")
                if layout.kind == "none":
                    return src, ArchiveResult(src.id)
                return src, await reader.month(src, layout, start, end)
            except Exception as exc:  # one broken site must not stop the backfill
                log.warning("archive discovery failed for %s: %r", src.id, exc)
                return src, ArchiveResult(src.id, error=repr(exc))

        found = await asyncio.gather(*(discover(s) for s in sources))
        candidates: dict[str, Candidate] = {}
        for src, arch in found:
            info[src.id].update(files=arch.files, missing_files=arch.missing, error=arch.error)
            per[src.id]["candidates"] = len(arch.candidates)
            for c in arch.candidates:
                candidates.setdefault(article_id(c.url), c)

        known = await storage.known_ids(candidates.keys(), include_old=False)
        selected = await asyncio.to_thread(_select, candidates, known, classifier, untitled, per)
        log.info("backfill %s: %d candidates, %d known, %d to download", f"{month:%Y-%m}", len(candidates),
                 len(known), len(selected))

        judge = Judge(classifier=classifier, storage=storage, tz=tz, run_id=run_id, by_id=by_id,
                      recent=await storage.titles_published(start - timedelta(days=3), end + timedelta(days=3)),
                      window_from=start - timedelta(days=1), window_to=end + timedelta(days=1), mark_old=False)
        seen: list[tuple[str, str, str]] = []

        async def fetch(c: Candidate):
            await gate.wait()
            return await _fetch_one(fetcher, c)

        tasks = [asyncio.create_task(fetch(c)) for c in selected]
        try:
            for next_done in asyncio.as_completed(tasks):
                c, html, ex = await next_done
                counts = per[c.source_id]
                counts["downloaded"] += ex is not None
                reason = fetcher.failures.get(c.url, "network_error")
                verdict = await judge.article(c, html, ex, reason)
                seen += verdict.seen
                if ex is None and reason not in _PERMANENT_FAILURES:
                    counts["temporary_failures"] += 1
                if verdict.record is None:
                    counts[verdict.outcome] += 1
                    result.outcome[verdict.outcome] += 1
                elif await _save(storage, verdict, opts.dry_run, result.outcome):
                    judge.saved_ids.add(verdict.record["id"])
                    counts["saved"] += 1
                    result.saved.append(verdict.record)
                if len(seen) >= 100 and not opts.dry_run:
                    await storage.mark_seen(seen)
                    seen = []
        finally:
            for t in tasks:
                t.cancel()
            if seen and not opts.dry_run:
                await storage.mark_seen(seen)
        ours = {host_of(src.homepage) for src in sources}
        result.stopped = {h: why for h, why in fetcher.stopped_hosts().items() if host_of(f"https://{h}") in ours}
        http_stats = dict(fetcher.stats)

    for src, arch in found:
        counts = per[src.id]
        stopped = any(host_of(src.homepage) == host_of(f"https://{h}") for h in result.stopped)
        if arch.error:
            status = "failed"
        elif info[src.id]["layout"] == "none" or arch.undated:
            status = "no_archive"
            if arch.undated:
                info[src.id]["layout"] = "undated"
        elif stopped or counts["temporary_failures"] or not finished_month:
            status = "partial"
        else:
            status = "done"
        info[src.id]["status"] = status
        result.sources[src.id] = {**info[src.id], **counts}
        if not opts.dry_run:
            await storage.save_backfill_progress({
                "source_id": src.id, "month": month, "status": status, "layout": info[src.id]["layout"],
                "candidates": counts["candidates"], "downloaded": counts["downloaded"], "saved": counts["saved"],
                "stats": result.sources[src.id], "run_id": run_id})

    finished = datetime.now(timezone.utc)
    result.duration = round((finished - started).total_seconds(), 1)
    stats = {"run_id": run_id, "started_at": started.isoformat(timespec="seconds"),
             "finished_at": finished.isoformat(timespec="seconds"), "duration_seconds": result.duration,
             "backfill": {"month": f"{month:%Y-%m}", "from": start.isoformat(), "to": end.isoformat()},
             "dry_run": opts.dry_run, "sources": result.sources, "outcome": dict(result.outcome),
             "http": http_stats, "block_log": str(block_log.path)}
    if result.stopped:
        stats["hosts_stopped"] = result.stopped
    await sync_to_db(storage, cfg.block_log_dir)
    if not opts.dry_run:
        await storage.save_run(run_id, stats["started_at"], stats["finished_at"], stats)
        name = f"{started.astimezone(tz):%Y-%m-%d_%H%M%S}_backfill-{month:%Y-%m}.json"
        await asyncio.to_thread(_write_run_log, cfg.data_dir / "logs" / "runs" / name, stats)
    return result
