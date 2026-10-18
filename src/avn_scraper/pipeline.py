"""The daily run: discover -> pre-filter -> fetch & extract -> classify -> store -> export."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .adapters import find_adapter
from .classify import Classifier
from .config import Config, Source
from .dedupe import find_cluster
from .discover import Candidate
from .discover.catchall import discover_catchall
from .discover.sources import EndpointCache, discover_source
from .export import export_all
from .extract import Extracted, extract
from .fetch import Fetcher
from .geo import detect_districts
from .normalize import detect_language
from .storage import LocalStorage
from .urls import article_id, host_of, same_site, slug_text

log = logging.getLogger(__name__)


@dataclass
class RunOptions:
    since_hours: float | None = None
    dry_run: bool = False
    prefilter: bool = True
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


async def _fetch_one(fetcher: Fetcher, c: Candidate) -> tuple[Candidate, str | None, Extracted | None]:
    """Download and extract one article. On failure the reason is in fetcher.failures[c.url]."""
    adapter = find_adapter(c.url)
    if adapter is not None:
        site, api_url = adapter
        resp = await fetcher.get(api_url)
        if resp is None:
            fetcher.failures[c.url] = fetcher.failures.get(api_url, "network_error")
            return c, None, None
        try:
            return c, resp.text, site.parse(resp.content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("%s adapter could not parse %s: %r", site.name, api_url, exc)
            fetcher.failures[c.url] = "adapter_error"
            return c, None, None

    resp = await fetcher.get(c.url)
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


async def run_pipeline(cfg: Config, opts: RunOptions,
                       transport: httpx.AsyncBaseTransport | None = None) -> RunResult:
    s = cfg.settings
    tz = ZoneInfo(cfg.timezone)
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    cutoff = started - timedelta(hours=opts.since_hours or float(s.get("window_hours", 30)))
    classifier = Classifier(cfg.keywords)
    storage = LocalStorage(cfg.data_dir)
    storage.upsert_sources(cfg.sources)
    stats: dict[str, Any] = {"run_id": run_id, "started_at": started.isoformat(timespec="seconds"),
                             "cutoff": cutoff.isoformat(timespec="seconds"), "dry_run": opts.dry_run}

    sources = [src for src in cfg.sources if src.enabled and (not opts.only_sources or src.id in opts.only_sources)]
    # Disabled outlets still name their catch-all results.
    by_id = {src.id: src for src in cfg.sources}
    catchall_cfg = s.get("search_catchall") or {}
    use_catchall = catchall_cfg.get("enabled", False) if opts.catchall is None else opts.catchall
    cache = EndpointCache(cfg.data_dir / "cache" / "endpoints.json", int(s.get("endpoint_cache_days", 7)))

    async with Fetcher(s.get("http") or {}, transport=transport) as fetcher:
        # 1. discover
        results = await asyncio.gather(*(discover_source(fetcher, src, cutoff, cache) for src in sources))
        cache.save()
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
        known = storage.known_ids(fresh.keys())
        new = {k: c for k, c in fresh.items() if k not in known}
        stats["new_candidates"] = len(new)
        if opts.prefilter:
            new = {k: c for k, c in new.items() if classifier.prefilter(c.title, c.summary, slug_text(c.url))}
        cap = int(s.get("max_articles_per_source", 80))
        per_source: Counter[str] = Counter()
        selected: list[Candidate] = []
        for c in sorted(new.values(), key=lambda c: c.published_at or started, reverse=True):
            if per_source[c.source_id] < cap:
                per_source[c.source_id] += 1
                selected.append(c)
        stats["selected_for_fetch"] = len(selected)
        log.info("%d candidates, %d new, %d selected for download", len(merged), stats["new_candidates"], len(selected))

        # 3. fetch & extract
        fetched = await asyncio.gather(*(_fetch_one(fetcher, c) for c in selected))
        stats["http"] = dict(fetcher.stats)
        failures = dict(fetcher.failures)

    # 4. classify, cluster, store
    recent = storage.recent_titles(days=3)
    saved: list[dict[str, Any]] = []
    seen_rows: list[tuple[str, str, str]] = []
    outcome: Counter[str] = Counter()
    failed: list[dict[str, str]] = []
    by_category: Counter[str] = Counter()
    scraped_at = datetime.now(timezone.utc)

    for c, html, ex in fetched:
        cid = article_id(c.url)
        fetch_ok = ex is not None
        reason = None if fetch_ok else failures.get(c.url, "network_error")
        # Permanently blocked pages (403, robots.txt, ...) are judged on the headline we
        # already have and not retried; temporary failures are retried tomorrow unless
        # the feed gave us a summary to judge by.
        permanent = reason in _PERMANENT_FAILURES
        if not fetch_ok:
            failed.append({"url": c.url, "reason": reason})
            if not c.title or not (permanent or c.summary):
                outcome["fetch_failed"] += 1
                if permanent:
                    seen_rows.append((cid, c.url, reason))
                continue
        ex = ex or Extracted(c.title, "", c.summary, "", None, None, None)
        published = ex.published_at or c.published_at
        if published and published > scraped_at + timedelta(minutes=15):
            # Some sites label Dhaka wall-clock time as UTC; read it as local time.
            published = published.replace(tzinfo=tz)
        if published:
            published = published.astimezone(timezone.utc)
        if published and published < cutoff:
            outcome["too_old"] += 1
            seen_rows.append((cid, c.url, "old"))
            continue

        title = ex.title or c.title
        summary = ex.summary or c.summary
        matches = classifier.classify(title, ex.body, summary)
        if not matches:
            if fetch_ok or permanent:
                outcome["no_match"] += 1
                seen_rows.append((cid, c.url, "no_match" if fetch_ok else f"no_match ({reason})"))
            else:
                outcome["fetch_failed"] += 1  # retried next run with the full text
            continue

        canonical = ex.canonical_url if ex.canonical_url and same_site(ex.canonical_url, c.url) else c.url
        aid = article_id(canonical)
        if aid != cid:
            seen_rows.append((cid, c.url, f"alias:{aid}"))
            if aid in storage.known_ids([aid]) or any(r["id"] == aid for r in saved):
                outcome["duplicate"] += 1
                continue

        src = by_id.get(c.source_id)
        district, mentioned = detect_districts(title, ex.body or summary)
        cluster = find_cluster(title, recent) or aid
        recent.append((aid, cluster, title))
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
            "published_date": (published or scraped_at).astimezone(tz).date().isoformat(),
            "scraped_at": scraped_at.isoformat(timespec="seconds"),
            "discovered_via": c.via,
            "district": district or (src.district if src else None),
            "districts_mentioned": mentioned,
            "story_cluster_id": cluster,
            "body_missing": not ex.body,
            "categories": [asdict(m) for m in matches],
            "run_id": run_id,
        }
        if not opts.dry_run:
            storage.save_article(record, html)
        saved.append(record)
        outcome["saved"] += 1
        if not ex.body:
            outcome["saved_headline_only"] += 1
        by_category.update(m.category for m in matches)

    if not opts.dry_run:
        storage.mark_seen(seen_rows)
    stats["outcome"] = dict(outcome)
    stats["fetch_failed"] = failed[:100]
    stats["by_category"] = dict(by_category)

    # 5. export + run log
    if not opts.dry_run and opts.export:
        stats["export"] = export_all(storage, cfg.data_dir / "exports", classifier.labels, cfg.timezone,
                                     day=started.astimezone(tz).date())
    finished = datetime.now(timezone.utc)
    stats["finished_at"] = finished.isoformat(timespec="seconds")
    stats["duration_seconds"] = round((finished - started).total_seconds(), 1)
    if not opts.dry_run:
        storage.save_run(run_id, stats["started_at"], stats["finished_at"], stats)
        log_dir = cfg.data_dir / "logs" / "runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        local = started.astimezone(tz)
        (log_dir / f"{local:%Y-%m-%d_%H%M%S}.json").write_text(
            json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    storage.close()
    return RunResult(run_id, stats, saved)


def reclassify(cfg: Config) -> Counter[str]:
    """Re-apply keywords.yaml to every stored article (no network)."""
    classifier = Classifier(cfg.keywords)
    storage = LocalStorage(cfg.data_dir)
    counts: Counter[str] = Counter()
    for art in list(storage.iter_articles()):
        matches = classifier.classify(art["title"] or "", art["body"] or "", art["summary"] or "")
        storage.replace_categories(art["id"], matches)
        counts["articles"] += 1
        counts["unmatched_now" if not matches else "matched"] += 1
        counts.update(f"cat:{m.category}" for m in matches)
    storage.close()
    return counts
