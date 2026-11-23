"""Backfill: scrape past months or days on demand (`avn backfill 2025-05`, `avn backfill
2024-08-05`, `avn backfill 2024-08-01..2024-08-07`), next to the daily run.

Old articles come from each outlet's archive sitemaps (discover/archive.py). Each period (a
month, a day or a range of days) is one run: discover -> skip known -> keyword pre-filter ->
download -> classify -> store, with the same rules as the daily run, saved article by article.
Progress is kept per outlet and period in `backfill_progress`, so an interrupted backfill
resumes. While the daily scrape (or `avn run`) holds the run lock, the backfill waits.
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
from .discover.archive import ArchiveLayout, ArchiveReader, ArchiveResult, discover_layout, headline_text, period_window
from .export import export_all
from .fetch import Fetcher
from .lock import is_locked
from .pipeline import _PERMANENT_FAILURES, Judge, _fetch_one, _save, _write_run_log, as_downloaded, by_host_turns
from .storage import PostgresStorage, Storage
from .geo import about_bangladesh
from .urls import article_id, host_of, international_section, off_topic_section, slug_text

log = logging.getLogger(__name__)

_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_DAY = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_QUARTER = re.compile(r"^(\d{4})-(\d{1,2})-q(\d)$", re.I)
# Quarters of a month (2024-08-q1): days 1-7, 8-15, 16-23 and 24 to the month's end.
_QUARTER_DAYS = ((1, 7), (8, 15), (16, 23), (24, None))
_MAX_DAY_RANGE = 31  # longer: use months
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


@dataclass(frozen=True, order=True)
class Period:
    """A stretch of past days to backfill: a whole month, one day or a range of days."""
    start: date
    end: date  # last day, included

    @property
    def is_month(self) -> bool:
        return self.start.day == 1 and self.end == _month_end(self.start)

    @property
    def label(self) -> str:
        if self.is_month:
            return f"{self.start:%Y-%m}"
        if (q := self._quarter()) is not None:
            return f"{self.start:%Y-%m}-q{q}"
        return f"{self.start}" if self.start == self.end else f"{self.start}..{self.end}"


    def _quarter(self) -> int | None:
        if (self.start.year, self.start.month) != (self.end.year, self.end.month):
            return None
        for n, (first, last) in enumerate(_QUARTER_DAYS, 1):
            if self.start.day == first and self.end == (self.end.replace(day=last) if last else _month_end(self.end)):
                return n
        return None


def _month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _day(spec: str) -> date:
    m = _DAY.match(spec)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        pass
    raise ValueError(f"not a day: {spec!r} (use YYYY-MM-DD)")


def _quarter(spec: str) -> tuple[date, date]:
    """'2024-08-q1' -> (2024-08-01, 2024-08-07); q4 runs to the month's last day."""
    m = _QUARTER.match(spec)
    if not m or not 1 <= int(m[2]) <= 12 or not 1 <= int(m[3]) <= 4:
        raise ValueError(f"not a quarter of a month: {spec!r} (use YYYY-MM-q1 .. q4)")
    first_day, last_day = _QUARTER_DAYS[int(m[3]) - 1]
    month = date(int(m[1]), int(m[2]), 1)
    return month.replace(day=first_day), month.replace(day=last_day) if last_day else _month_end(month)


def parse_periods(specs: list[str], today: date) -> list[Period]:
    """Months as in parse_months, plus days: '2024-08-05' (one day), '2024-08-01..2024-08-07'
    (one period of days, at most 31) and quarters of a month, '2024-08-q1' .. 'q4' (days 1-7,
    8-15, 16-23, 24-end; '2024-08-q1..2024-08-q2' is days 1-15). Days must be before today."""
    periods: set[Period] = set()
    months: list[str] = []
    for spec in specs:
        for part in (p.strip() for p in spec.split(",") if p.strip()):
            ends = [x.strip() for x in part.split("..", 1)]
            if all(_QUARTER.match(x) for x in ends):  # quarters become their days
                ends = [str(_quarter(ends[0])[0]), str(_quarter(ends[-1])[1])]
                part = "..".join(ends)
            if not all(_DAY.match(x) for x in ends):
                months.append(part)
                continue
            first, last = _day(ends[0]), _day(ends[-1])
            if last < first:
                raise ValueError(f"range goes backwards: {part!r}")
            if last >= today:
                raise ValueError(f"{part}: only days before today (the daily run covers today)")
            if (last - first).days + 1 > _MAX_DAY_RANGE:
                raise ValueError(f"{part}: more than {_MAX_DAY_RANGE} days; use months, e.g. 2024-08")
            periods.add(Period(first, last))
    if months:  # the current month stays one period: its window stops at yesterday, status partial
        periods.update(Period(m, _month_end(m)) for m in parse_months(months, today))
    return sorted(periods)


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
        elif off_topic_section(c.url):  # sports, entertainment, business, ...
            per[c.source_id]["off_topic"] += 1
        elif international_section(c.url) and not about_bangladesh(f"{c.title} {c.summary} {slug_text(c.url)}"):
            per[c.source_id]["foreign"] += 1  # world news not about Bangladesh
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
    periods: list[Period]
    only_sources: list[str] | None = None
    dry_run: bool = False
    redo: bool = False  # also redo outlet-periods already done
    untitled: str | None = None  # None = settings.yaml backfill.untitled


@dataclass
class PeriodResult:
    period: Period
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


def _http_settings(settings: dict[str, Any], sources: list[Source]) -> dict[str, Any]:
    """The daily run's HTTP settings, with the backfill's pace and stricter breaker.

    Every site waits backfill.per_host_delay between requests, except backfill.fast_sources
    (outlets with a clean block record), which wait backfill.fast_delay, never less than the
    daily run's per_host_delay. `fetch: impersonate` outlets are never fast. A site is stopped
    after backfill.block_threshold blocks in the run, not only in a row."""
    http = dict(settings.get("http") or {})
    bf = settings.get("backfill") or {}
    daily = float(http.get("per_host_delay", 1.0))
    http["per_host_delay"] = max(daily, float(bf.get("per_host_delay", 2.0)))
    fast_delay = max(daily, float(bf.get("fast_delay", http["per_host_delay"])))
    by_id = {s.id: s for s in sources}
    host_delays: dict[str, float] = {}
    for sid in bf.get("fast_sources") or []:
        src = by_id.get(sid)
        if src is None:
            log.warning("backfill.fast_sources: no outlet %r in sources.yaml; ignored", sid)
        elif src.fetch == "impersonate":
            log.warning("backfill.fast_sources: %s uses fetch: impersonate, never fast; ignored", sid)
        elif host := urlsplit(src.homepage).hostname:
            host_delays[host] = fast_delay
    http["host_delays"] = host_delays
    http["block_threshold"] = min(int(http.get("block_threshold", 3)), int(bf.get("block_threshold", 2)))
    http["block_strikes_reset"] = False
    return http


async def run_backfill(cfg: Config, opts: BackfillOptions, transport: httpx.AsyncBaseTransport | None = None,
                       gate: RunGate | None = None,
                       on_period: Callable[[PeriodResult], None] | None = None) -> list[PeriodResult]:
    untitled = opts.untitled or (cfg.settings.get("backfill") or {}).get("untitled", "download")
    if untitled not in UNTITLED:
        raise ValueError(f"untitled must be one of {UNTITLED}")
    storage = await PostgresStorage.open(cfg.data_dir)
    gate = gate or RunGate(cfg.data_dir / ".run.lock",
                           float((cfg.settings.get("backfill") or {}).get("pause_poll_seconds", 30)))
    classifier = await asyncio.to_thread(Classifier, cfg.keywords)
    layouts: dict[str, ArchiveLayout] = {}
    reader = ArchiveReader()
    results: list[PeriodResult] = []
    try:
        if not opts.dry_run:
            await storage.upsert_sources(cfg.sources)
        for period in opts.periods:
            result = await _backfill_period(cfg, opts, storage, period, untitled, classifier, layouts, reader,
                                            transport, gate)
            results.append(result)
            if on_period:
                on_period(result)
        if not opts.dry_run and any(r.outcome.get("saved") for r in results):
            await export_all(storage, cfg.data_dir / "exports", classifier.labels, cfg.timezone)
    finally:
        await storage.close()
    return results


async def _backfill_period(cfg: Config, opts: BackfillOptions, storage: Storage, period: Period, untitled: str,
                           classifier: Classifier, layouts: dict[str, ArchiveLayout], reader: ArchiveReader,
                           transport: httpx.AsyncBaseTransport | None, gate: RunGate) -> PeriodResult:
    tz = ZoneInfo(cfg.timezone)
    today = datetime.now(tz).date()
    start, end = period_window(period, tz, today)
    result = PeriodResult(period)
    finished = end.date() > period.end  # the whole period is in the past
    done = set() if opts.redo else {r["source_id"] for r in await storage.backfill_progress([period], exact=True)
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

    async with Fetcher(_http_settings(cfg.settings, cfg.sources), transport=transport, sources=cfg.sources,
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
        log.info("backfill %s: %d candidates, %d known, %d to download", period.label, len(candidates),
                 len(known), len(selected))

        judge = Judge(classifier=classifier, storage=storage, tz=tz, run_id=run_id, by_id=by_id,
                      recent=await storage.titles_published(start - timedelta(days=3), end + timedelta(days=3)),
                      window_from=start - timedelta(days=1), window_to=end + timedelta(days=1), mark_old=False)
        seen: list[tuple[str, str, str]] = []

        async def fetch(c: Candidate):
            await gate.wait()
            return await _fetch_one(fetcher, c)

        downloads = as_downloaded([fetch(c) for c in by_host_turns(selected)])
        try:
            async for c, html, ex in downloads:
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
            await downloads.aclose()
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
        elif stopped or counts["temporary_failures"] or not finished:
            status = "partial"
        else:
            status = "done"
        info[src.id]["status"] = status
        result.sources[src.id] = {**info[src.id], **counts}
        if not opts.dry_run:
            await storage.save_backfill_progress({
                "source_id": src.id, "month": period.start, "until": period.end, "status": status, "layout": info[src.id]["layout"],
                "candidates": counts["candidates"], "downloaded": counts["downloaded"], "saved": counts["saved"],
                "stats": result.sources[src.id], "run_id": run_id})

    finished_at = datetime.now(timezone.utc)
    result.duration = round((finished_at - started).total_seconds(), 1)
    stats = {"run_id": run_id, "started_at": started.isoformat(timespec="seconds"),
             "finished_at": finished_at.isoformat(timespec="seconds"), "duration_seconds": result.duration,
             "backfill": {"period": period.label, "month": f"{period.start:%Y-%m}",
                          "from": start.isoformat(), "to": end.isoformat()},
             "dry_run": opts.dry_run, "sources": result.sources, "outcome": dict(result.outcome),
             "http": http_stats, "block_log": str(block_log.path)}
    if result.stopped:
        stats["hosts_stopped"] = result.stopped
    await sync_to_db(storage, cfg.block_log_dir)
    if not opts.dry_run:
        await storage.save_run(run_id, stats["started_at"], stats["finished_at"], stats)
        name = f"{started.astimezone(tz):%Y-%m-%d_%H%M%S}_backfill-{period.label.replace('..', '_')}.json"
        await asyncio.to_thread(_write_run_log, cfg.data_dir / "logs" / "runs" / name, stats)
    return result
