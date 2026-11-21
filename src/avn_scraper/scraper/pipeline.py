"""The daily run: discover -> pre-filter -> fetch & extract -> classify -> store -> export."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import psycopg

from .adapters import find_adapter
from .classify import Classifier
from .config import Config, Source
from .dedupe import find_cluster
from .discover import Candidate
from .discover.catchall import discover_catchall
from .discover.sources import EndpointCache, ListingMemory, discover_source
from .export import export_all
from .extract import Extracted, extract
from .blocklog import BlockLog, sync_to_db
from .fetch import Fetcher
from .geo import detect_districts
from .normalize import detect_language
from .storage import PostgresStorage, Storage
from .urls import article_id, host_of, same_site, slug_text

log = logging.getLogger(__name__)


@dataclass
class RunOptions:
    since_hours: float | None = None
    since_last_run: bool = False  # look back to the last full run, at least window_hours
    dry_run: bool = False
    prefilter: bool | None = None  # None = settings.yaml `prefilter`
    only_sources: list[str] | None = None
    catchall: bool | None = None  # None = use settings.yaml
    export: bool = True


@dataclass
class RunResult:
    run_id: str
    stats: dict[str, Any]
    saved: list[dict[str, Any]] = field(default_factory=list)


def _merge(candidates: list[Candidate]) -> dict[str, Candidate]:
    """One candidate per article id, keeping the richest title/summary/date."""
    merged: dict[str, Candidate] = {}
    for c in candidates:
        key = article_id(c.url)
        if key not in merged:
            merged[key] = c
            continue
        m = merged[key]
        if len(c.title) > len(m.title):
            m.title = c.title
        if len(c.summary) > len(m.summary):
            m.summary = c.summary
        m.published_at = m.published_at or c.published_at
        if m.source_id.startswith("web:") and not c.source_id.startswith("web:"):
            m.source_id = c.source_id
    return merged


def _assign_sources(candidates: list[Candidate], sources: list[Source]) -> None:
    """Catch-all results that belong to a configured outlet get that outlet's id."""
    for c in candidates:
        if c.source_id.startswith("web:"):
            owner = next((s for s in sources if same_site(c.url, s.homepage)), None)
            if owner:
                c.source_id = owner.id


# Failures that will not go away by retrying tomorrow (vs. timeouts, 5xx, 429).
_PERMANENT_FAILURES = {"http_401", "http_403", "http_404", "http_410", "http_451", "robots_blocked",
                       "BlockedURL", "UnsupportedEncoding", "ResponseTooLarge", "not_html",
                       "extract_error", "adapter_error"}


def _cap_per_source(candidates: list[Candidate], cap: Any, now: datetime) -> tuple[list[Candidate], dict[str, int]]:
    """Newest first, at most `cap` per outlet. No cap (None or 0) keeps every one.
    Returns the selection and, per outlet, how many were left out."""
    if not cap:
        return candidates, {}
    per_source: Counter[str] = Counter()
    selected: list[Candidate] = []
    left_out: Counter[str] = Counter()
    for c in sorted(candidates, key=lambda c: c.published_at or now, reverse=True):
        if per_source[c.source_id] < int(cap):
            per_source[c.source_id] += 1
            selected.append(c)
        else:
            left_out[c.source_id] += 1
    return selected, dict(left_out)


def _keyword_hits(classifier: Classifier, candidates: dict[str, Candidate]) -> dict[str, Candidate]:
    return {k: c for k, c in candidates.items() if classifier.prefilter(c.title, c.summary, slug_text(c.url))}


async def _fetch_one(fetcher: Fetcher, c: Candidate) -> tuple[Candidate, str | None, Extracted | None]:
    """Download and extract one article. On failure the reason is in fetcher.failures[c.url]."""
    adapter = find_adapter(c.url)
    if adapter is not None:
        site, api_url = adapter
        resp = await fetcher.get(api_url, "api")
        if resp is None:
            fetcher.failures[c.url] = fetcher.failures.get(api_url, "network_error")
            return c, None, None
        try:
            return c, resp.text, await asyncio.to_thread(site.parse, resp.content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("%s adapter could not parse %s: %r", site.name, api_url, exc)
            fetcher.failures[c.url] = "adapter_error"
            return c, None, None

    resp = await fetcher.get(c.url, "article")
    if resp is None:
        return c, None, None
    if "html" not in resp.headers.get("content-type", "html"):
        fetcher.failures[c.url] = "not_html"
        return c, None, None
    html = resp.text
    try:
        ex = await asyncio.to_thread(extract, html, str(resp.url))
    except Exception as exc:
        log.warning("extraction failed for %s: %r", c.url, exc)
        fetcher.failures[c.url] = "extract_error"
        return c, html, None
    return c, html, ex


def lookback_hours(window: float, last_run: datetime | None, now: datetime) -> float:
    """Hours back to `last_run`, never fewer than `window`."""
    if last_run is None:
        return window
    return max(window, (now - last_run).total_seconds() / 3600)


@dataclass
class Verdict:
    """What to do with one downloaded (or failed) candidate."""
    outcome: str  # saved | fetch_failed | too_old | too_new | no_match | duplicate
    record: dict[str, Any] | None = None
    html: str | None = None
    seen: list[tuple[str, str, str]] = field(default_factory=list)  # rows for seen_urls
    failed: dict[str, str] | None = None


@dataclass
class Judge:
    """Turns a download into an article record: date window, classification, aliases,
    districts and story cluster. Shared by the daily run and the backfill."""
    classifier: Classifier
    storage: Storage
    tz: ZoneInfo
    run_id: str
    by_id: dict[str, Source]
    recent: list[tuple[str, str, str]]  # (id, cluster, title) to cluster against
    window_from: datetime
    window_to: datetime | None = None  # backfill only: articles from later are skipped
    mark_old: bool = True  # daily run: remember too-old links; a backfill must not
    saved_ids: set[str] = field(default_factory=set)
    scraped_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    async def article(self, c: Candidate, html: str | None, ex: Extracted | None, reason: str) -> Verdict:
        cid = article_id(c.url)
        fetch_ok = ex is not None
        reason = None if fetch_ok else reason
        # Permanently blocked pages (403, robots.txt, ...) are judged on the headline we
        # already have and not retried; temporary failures are retried tomorrow unless
        # the feed gave us a summary to judge by.
        permanent = reason in _PERMANENT_FAILURES
        failed = None if fetch_ok else {"url": c.url, "reason": reason}
        if not fetch_ok and (not c.title or not (permanent or c.summary)):
            return Verdict("fetch_failed", seen=[(cid, c.url, reason)] if permanent else [], failed=failed)
        ex = ex or Extracted(c.title, "", c.summary, "", None, None, None)
        date_only = ex.date_only and ex.published_at is not None
        if date_only and c.published_at:  # the feed's full timestamp beats a bare day
            date_only = False
            published = c.published_at
        else:
            published = ex.published_at or c.published_at
        if date_only:
            # A bare day: the story is in the window when its day (in Dhaka) is not before
            # the window's first day. Midnight would drop yesterday's afternoon stories.
            published = datetime.combine(published.date(), datetime.min.time(), self.tz)
            too_old = published.date() < self.window_from.astimezone(self.tz).date()
        else:
            if published and published > self.scraped_at + timedelta(minutes=15):
                # Some sites label Dhaka wall-clock time as UTC; read it as local time.
                published = published.replace(tzinfo=self.tz)
            too_old = bool(published and published < self.window_from)
        if published:
            published = published.astimezone(timezone.utc)
        if too_old:
            return Verdict("too_old", seen=[(cid, c.url, "old")] if self.mark_old else [], failed=failed)
        if published and self.window_to and published >= self.window_to:
            return Verdict("too_new", failed=failed)

        title = ex.title or c.title
        summary = ex.summary or c.summary
        matches = await asyncio.to_thread(self.classifier.classify, title, ex.body, summary)
        if not matches:
            if fetch_ok or permanent:
                return Verdict("no_match", seen=[(cid, c.url, "no_match" if fetch_ok else f"no_match ({reason})")],
                               failed=failed)
            return Verdict("fetch_failed", failed=failed)  # retried next run with the full text

        seen: list[tuple[str, str, str]] = []
        canonical = ex.canonical_url if ex.canonical_url and same_site(ex.canonical_url, c.url) else c.url
        aid = article_id(canonical)
        if aid != cid:
            seen.append((cid, c.url, f"alias:{aid}"))
            if aid in self.saved_ids or aid in await self.storage.known_ids([aid]):
                return Verdict("duplicate", seen=seen, failed=failed)

        src = self.by_id.get(c.source_id)
        district, mentioned = await asyncio.to_thread(detect_districts, title, ex.body or summary)
        cluster = await asyncio.to_thread(find_cluster, title, self.recent) or aid
        self.recent.append((aid, cluster, title))
        record = {
            "id": aid,
            "url": c.url,
            "canonical_url": canonical,
            "source_id": c.source_id,
            "source_name": src.name if src else host_of(c.url),
            "source_type": src.type if src else "web",
            "language": src.language if src else detect_language(f"{title} {ex.body}"),
            "title": title,
            "summary": summary,
            "body": ex.body,
            "author": ex.author,
            "image": ex.image,
            "published_at": published.isoformat() if published else None,
            "published_date": (published or self.scraped_at).astimezone(self.tz).date().isoformat(),
            "scraped_at": self.scraped_at.isoformat(timespec="seconds"),
            "discovered_via": c.via,
            "district": district or (src.district if src else None),
            "districts_mentioned": mentioned,
            "story_cluster_id": cluster,
            "body_missing": not ex.body,
            "categories": [asdict(m) for m in matches],
            "run_id": self.run_id,
        }
        return Verdict("saved", record=record, html=html, seen=seen, failed=failed)


async def _save(storage: Storage, verdict: Verdict, dry_run: bool, outcome: Counter[str]) -> bool:
    """Store a matched article (not on a dry run). False if the database refused it."""
    if not dry_run:
        try:
            await storage.save_article(verdict.record, verdict.html)
        except psycopg.OperationalError:
            raise  # the database went away: stop, the next run covers these articles
        except psycopg.Error as exc:
            log.error("could not save %s: %r", verdict.record["url"], exc)
            outcome["db_failed"] += 1
            return False
    outcome["saved"] += 1
    if verdict.record["body_missing"]:
        outcome["saved_headline_only"] += 1
    return True


async def run_pipeline(cfg: Config, opts: RunOptions,
                       transport: httpx.AsyncBaseTransport | None = None) -> RunResult:
    """Fails before any web request if the database cannot be used."""
    storage = await PostgresStorage.open(cfg.data_dir)
    try:
        return await _run(cfg, opts, storage, transport)
    finally:
        await storage.close()


async def _run(cfg: Config, opts: RunOptions, storage: Storage,
               transport: httpx.AsyncBaseTransport | None) -> RunResult:
    s = cfg.settings
    tz = ZoneInfo(cfg.timezone)
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    hours = opts.since_hours or float(s.get("window_hours", 24))
    if opts.since_last_run:
        hours = lookback_hours(hours, await storage.last_full_run(), started)
    cutoff = started - timedelta(hours=hours)
    classifier = await asyncio.to_thread(Classifier, cfg.keywords)
    if not opts.dry_run:
        await storage.upsert_sources(cfg.sources)
    stats: dict[str, Any] = {"run_id": run_id, "started_at": started.isoformat(timespec="seconds"),
                             "cutoff": cutoff.isoformat(timespec="seconds"), "dry_run": opts.dry_run}
    if opts.only_sources:  # a partial run must not count as the last full run
        stats["only_sources"] = opts.only_sources

    sources = [src for src in cfg.sources if src.enabled and (not opts.only_sources or src.id in opts.only_sources)]
    # Disabled outlets still name their catch-all results.
    by_id = {src.id: src for src in cfg.sources}
    catchall_cfg = s.get("search_catchall") or {}
    use_catchall = catchall_cfg.get("enabled", False) if opts.catchall is None else opts.catchall
    cache = await asyncio.to_thread(EndpointCache, cfg.data_dir / "cache" / "endpoints.json",
                                    int(s.get("endpoint_cache_days", 7)))
    memory = await asyncio.to_thread(ListingMemory, cfg.data_dir / "cache" / "listing_seen.json")

    block_log = BlockLog.for_run(cfg.block_log_dir, run_id, "dry-run" if opts.dry_run else "run", cfg.timezone)
    block_log.window = (cutoff, started)
    stats["block_log"] = str(block_log.path)
    async with Fetcher(s.get("http") or {}, transport=transport, sources=cfg.sources,
                       cooldown_path=cfg.host_blocks_path, block_log=block_log) as fetcher:
        # 1. discover
        results = await asyncio.gather(*(discover_source(fetcher, src, cutoff, cache, s.get("sections"), memory)
                                         for src in sources))
        await asyncio.to_thread(cache.save)
        stats["sources"] = {r.source_id: {"ok": r.ok, "counts": r.counts, "endpoints": r.endpoints, "error": r.error}
                            for r in results}
        candidates = [c for r in results for c in r.candidates]
        if use_catchall:
            extra = await discover_catchall(fetcher, cfg.keywords.get("catchall_queries") or [])
            stats["catchall_candidates"] = len(extra)
            _assign_sources(extra, cfg.sources)
            if opts.only_sources:  # keep only results from the requested outlets
                extra = [c for c in extra if c.source_id in opts.only_sources]
            candidates += extra

        # 2. filter: fresh, unseen, keyword pre-filter, per-outlet cap
        merged = _merge(candidates)
        stats["candidates"] = len(merged)
        fresh = {k: c for k, c in merged.items() if not c.published_at or c.published_at >= cutoff}
        known = await storage.known_ids(fresh.keys())
        new = {k: c for k, c in fresh.items() if k not in known}
        stats["new_candidates"] = len(new)
        prefilter = bool(s.get("prefilter", False)) if opts.prefilter is None else opts.prefilter
        if prefilter:
            new = await asyncio.to_thread(_keyword_hits, classifier, new)
        selected, capped = _cap_per_source(list(new.values()), s.get("max_articles_per_source"), started)
        stats["selected_for_fetch"] = len(selected)
        if capped:
            stats["capped"] = capped
            log.warning("Per-outlet cap left out %d articles: %s", sum(capped.values()), capped)
        log.info("%d candidates, %d new, %d selected for download", len(merged), stats["new_candidates"], len(selected))

        # 3. fetch & extract
        fetched = await asyncio.gather(*(_fetch_one(fetcher, c) for c in selected))
        stats["http"] = dict(fetcher.stats)
        if fetcher.stopped_hosts():
            stats["hosts_stopped"] = fetcher.stopped_hosts()
        # Hosts that refused or failed us, kept in the run record (JSON file and Postgres).
        troubled = {h: t for h, t in block_log.summary().items() if t["blocks"] or t["errors"] or t["stopped"]}
        if troubled:
            stats["blocks"] = troubled
        failures = dict(fetcher.failures)

    # 4. classify, cluster, store
    judge = Judge(classifier=classifier, storage=storage, tz=tz, run_id=run_id, by_id=by_id,
                  recent=await storage.recent_titles(days=3), window_from=cutoff)
    saved: list[dict[str, Any]] = []
    seen_rows: list[tuple[str, str, str]] = []
    outcome: Counter[str] = Counter()
    failed: list[dict[str, str]] = []
    by_category: Counter[str] = Counter()

    for c, html, ex in fetched:
        verdict = await judge.article(c, html, ex, failures.get(c.url, "network_error"))
        seen_rows += verdict.seen
        if verdict.failed:
            failed.append(verdict.failed)
        if verdict.record is None:
            outcome[verdict.outcome] += 1
            continue
        if not await _save(storage, verdict, opts.dry_run, outcome):
            continue
        judge.saved_ids.add(verdict.record["id"])
        saved.append(verdict.record)
        by_category.update(m["category"] for m in verdict.record["categories"])

    if not opts.dry_run:
        await storage.mark_seen(seen_rows)
        # Saved only once the articles are stored: the next run's paging stops at these links.
        await asyncio.to_thread(memory.save)
    stats["outcome"] = dict(outcome)
    stats["fetch_failed"] = failed[:100]
    stats["by_category"] = dict(by_category)

    # 5. export + run log
    if not opts.dry_run and opts.export:
        stats["export"] = await export_all(storage, cfg.data_dir / "exports", classifier.labels, cfg.timezone,
                                           day=started.astimezone(tz).date())
    finished = datetime.now(timezone.utc)
    stats["finished_at"] = finished.isoformat(timespec="seconds")
    stats["duration_seconds"] = round((finished - started).total_seconds(), 1)
    # Block logs (this run's file and any earlier one not imported yet) go to Postgres,
    # dry runs included: they are a record of how sites treated us, not scraped data.
    imported = await sync_to_db(storage, cfg.block_log_dir)
    if imported:
        log.info("block logs imported into Postgres: %s", ", ".join(imported))
    if not opts.dry_run:
        await storage.save_run(run_id, stats["started_at"], stats["finished_at"], stats)
        log_name = f"{started.astimezone(tz):%Y-%m-%d_%H%M%S}.json"
        await asyncio.to_thread(_write_run_log, cfg.data_dir / "logs" / "runs" / log_name, stats)
    return RunResult(run_id, stats, saved)


def _write_run_log(path: Path, stats: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")


async def reclassify(cfg: Config) -> Counter[str]:
    """Re-apply keywords.yaml to every stored article (no network)."""
    classifier = Classifier(cfg.keywords)
    storage = await PostgresStorage.open(cfg.data_dir)
    counts: Counter[str] = Counter()
    try:
        async for art in storage.iter_articles():
            matches = classifier.classify(art["title"] or "", art["body"] or "", art["summary"] or "")
            await storage.replace_categories(art["id"], matches)
            counts["articles"] += 1
            counts["unmatched_now" if not matches else "matched"] += 1
            counts.update(f"cat:{m.category}" for m in matches)
    finally:
        await storage.close()
    return counts
