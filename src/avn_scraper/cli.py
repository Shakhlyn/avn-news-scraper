"""Command line: `avn run`, `avn check-sources`, `avn export`, `avn reclassify`, `avn stats`,
`avn migrate`, `avn import-sqlite`, `avn serve`."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
import typer
from pydantic import ValidationError
from pydantic_settings import SettingsError
from rich.console import Console
from rich.table import Table

from .scraper.classify import Classifier
from .scraper.config import load_config
from .scraper.discover.sources import EndpointCache, ListingMemory, discover_source
from .scraper.export import export_all
from .scraper.blocklog import BlockLog, read_events, sync_to_db
from .scraper.fetch import Fetcher
from .scraper.lock import RunInProgress, run_lock
from .scraper.pipeline import RunOptions, reclassify, run_pipeline
from .scraper.importer import import_sqlite
from .scraper.storage import DatabaseUnavailable, PostgresStorage, connect, migrate as apply_migrations

app = typer.Typer(add_completion=False, help="Bangladesh human-rights news scraper.")
console = Console()

ConfigOpt = typer.Option(None, "--config", "-c", help="Config directory (default: ./config)")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "htmldate", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.ERROR)


async def _storage(cfg) -> PostgresStorage:
    try:
        return await PostgresStorage.open(cfg.data_dir)
    except DatabaseUnavailable as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)


def _split(value: Optional[str]) -> Optional[list[str]]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


@app.command()
def run(
    since: Optional[float] = typer.Option(None, help="Look back this many hours (default: settings window_hours)"),
    since_last_run: bool = typer.Option(False, "--since-last-run", help="Look back to the last full run, at least window_hours"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Do everything except saving; print matches"),
    sources: Optional[str] = typer.Option(None, help="Comma separated source ids to limit the run"),
    no_prefilter: bool = typer.Option(False, "--no-prefilter", help="Download every new article, not just keyword hits (slow)"),
    catchall: Optional[bool] = typer.Option(None, "--catchall/--no-catchall", help="Override search_catchall.enabled"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Scrape, classify and store today's relevant news."""
    _setup_logging(verbose)
    cfg = load_config(config)
    if since is not None and since_last_run:
        console.print("[red]Use --since or --since-last-run, not both.[/]")
        raise typer.Exit(2)
    opts = RunOptions(since_hours=since, since_last_run=since_last_run, dry_run=dry_run, prefilter=not no_prefilter,
                      only_sources=_split(sources), catchall=catchall)
    try:
        with run_lock(cfg.data_dir / ".run.lock"):
            result = asyncio.run(run_pipeline(cfg, opts))
    except RunInProgress as exc:
        console.print(f"[yellow]A scrape is already running ({exc}); not starting another.[/]")
        raise typer.Exit(1)
    except DatabaseUnavailable as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    except psycopg.OperationalError as exc:
        console.print(f"[red]Lost the database during the run ({exc}). Run again once it is back.[/]")
        raise typer.Exit(1)

    st = result.stats
    failed = [sid for sid, info in st.get("sources", {}).items() if not info["ok"]]
    console.print(f"[bold]Run {result.run_id}[/] in {st['duration_seconds']}s - "
                  f"{st.get('candidates', 0)} candidates, {st.get('selected_for_fetch', 0)} downloaded, "
                  f"outcome {st.get('outcome', {})}")
    if "catchall_candidates" in st:
        console.print(f"Catch-all search returned {st['catchall_candidates']} items")
    if st.get("by_category"):
        console.print("By category:", st["by_category"])
    if failed:
        console.print(f"[yellow]Sources with no results ({len(failed)}):[/] {', '.join(failed)}")
    if st.get("fetch_failed"):
        reasons = Counter(f["reason"] for f in st["fetch_failed"])
        console.print(f"Downloads that failed: {dict(reasons)} "
                      "(blocked pages are judged by headline; temporary errors retry next run)")
    for host, why in (st.get("hosts_stopped") or {}).items():
        console.print(f"[red]Not fetched: {host}: {why}[/]")
    if result.saved:
        title = "Would save (dry run)" if dry_run else "Saved to Postgres"
        table = Table("date", "source", "categories", "title", title=title, show_lines=False)
        for r in result.saved:
            table.add_row(r["published_date"], r["source_name"],
                          ", ".join(f"{c['category']}({c['score']})" for c in r["categories"]),
                          r["title"][:90])
        console.print(table)


async def _sync_block_logs(cfg, quiet: bool = False) -> list[str]:
    """Import finished block-log files not yet in Postgres. quiet: skip silently when it is down."""
    try:
        storage = await PostgresStorage.open(cfg.data_dir)
    except DatabaseUnavailable as exc:
        if not quiet:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1)
        console.print("[yellow]Postgres not reachable; block logs stay in files and are imported next time.[/]")
        return []
    try:
        return await sync_to_db(storage, cfg.block_log_dir)
    finally:
        await storage.close()


@app.command("sync-block-logs")
def sync_block_logs(config: Optional[Path] = ConfigOpt) -> None:
    """Import finished block logs (data/logs/blocks) not yet in Postgres. Scrapes do this at the end."""
    cfg = load_config(config)
    imported = asyncio.run(_sync_block_logs(cfg))
    console.print(f"Imported {len(imported)} block log(s)" + (f": {', '.join(imported)}" if imported else ""))


@app.command("block-report")
def block_report(
    days: int = typer.Option(30, help="Look at runs from the last N days"),
    host: Optional[str] = typer.Option(None, help="Only this host (e.g. www.kalbela.com)"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Summarise the block logs (data/logs/blocks): who blocks us, how, and after how many requests."""
    cfg = load_config(config)
    since = (datetime.now(ZoneInfo(cfg.timezone)) - timedelta(days=days)).date()
    events = read_events(cfg.block_log_dir, since)
    if host:
        events = [e for e in events if e.get("host") == host or e.get("event") in ("run_start", "run_end")]
    runs = [e for e in events if e["event"] == "run_end"]
    if not runs:
        console.print(f"No block logs in {cfg.block_log_dir} for the last {days} days.")
        return
    per: dict[str, dict] = {}
    for run in runs:
        for h, t in run["hosts"].items():
            if host and h != host:
                continue
            p = per.setdefault(h, {"source": t["source"], "client": t["client"], "runs": 0, "requests": 0, "ok": 0,
                                   "blocks": Counter(), "errors": 0, "stopped": 0, "first": []})
            p["runs"] += 1
            p["requests"] += t["requests"]
            p["ok"] += t["ok"]
            p["blocks"].update(t["blocks"])
            p["errors"] += t["errors"]
            p["stopped"] += bool(t["stopped"])
            if t["first_block_seq"]:
                p["first"].append(t["first_block_seq"])
    modes = Counter(r["mode"] for r in runs)
    console.print(f"{len(runs)} runs in the last {days} days: {dict(modes)}")
    table = Table("host", "source", "client", "runs", "requests", "blocked", "block %", "errors", "stopped",
                  "1st block at req #", title="Per host (hosts with blocks or errors)")
    troubled = {h: p for h, p in per.items() if p["blocks"] or p["errors"] or p["stopped"]}
    for h, p in sorted(troubled.items(), key=lambda kv: -sum(kv[1]["blocks"].values())):
        blocked = sum(p["blocks"].values())
        first = sorted(p["first"])
        table.add_row(h, p["source"] or "-", p["client"], str(p["runs"]), str(p["requests"]),
                      ", ".join(f"{k} {v}" for k, v in p["blocks"].items()) or "0",
                      f"{100 * blocked / max(p['requests'], 1):.1f}", str(p["errors"]), str(p["stopped"]),
                      f"median {first[len(first) // 2]}, min {first[0]}" if first else "-")
    console.print(table if troubled else "No blocks or errors.")
    fails = [e for e in events if e["event"] == "request"]
    if fails:
        groups = Counter((e["host"], e.get("purpose"), e["outcome"], e.get("block") or e.get("error", "")[:30],
                          str(e.get("status", "-")), (e.get("title") or "")[:30]) for e in fails)
        table = Table("count", "host", "purpose", "outcome", "kind / error", "status", "page title",
                      title="Failed requests by kind")
        for key, n in groups.most_common(25):
            table.add_row(str(n), *key)
        console.print(table)
        tz = ZoneInfo(cfg.timezone)
        hours = Counter(datetime.fromisoformat(e["ts"]).astimezone(tz).hour for e in fails if e["outcome"] == "block")
        if hours:
            console.print("Blocks by hour (" + cfg.timezone + "): "
                          + "  ".join(f"{h:02d}h:{n}" for h, n in sorted(hours.items())))
    stops = [e for e in events if e["event"] == "host_stopped"]
    for e in stops[-10:]:
        console.print(f"[red]{e['ts'][:16]} stopped {e['host']}[/] after {e['requests_so_far']} requests "
                      f"({e['ok_so_far']} ok): {e['reason']}")


@app.command("check-sources")
def check_sources(
    sources: Optional[str] = typer.Option(None, help="Comma separated source ids"),
    refresh: bool = typer.Option(False, help="Ignore cached auto-discovered feeds/sitemaps"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Show, per outlet, how many recent items each discovery method finds."""
    _setup_logging(verbose)
    cfg = load_config(config)
    only = _split(sources)
    selected = [s for s in cfg.sources if s.enabled and (not only or s.id in only)]
    cache = EndpointCache(cfg.data_dir / "cache" / "endpoints.json", int(cfg.settings.get("endpoint_cache_days", 7)))
    if refresh:
        for s in selected:
            cache.clear(s.id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(cfg.settings.get("window_hours", 24)))
    memory = ListingMemory(cfg.data_dir / "cache" / "listing_seen.json")  # read only: never saved here

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    block_log = BlockLog.for_run(cfg.block_log_dir, run_id, "check-sources", cfg.timezone)

    async def go():
        async with Fetcher(cfg.settings.get("http") or {}, sources=cfg.sources,
                           cooldown_path=cfg.host_blocks_path, block_log=block_log) as fetcher:
            found = await asyncio.gather(*(discover_source(fetcher, s, cutoff, cache, cfg.settings.get("sections"), memory)
                                           for s in selected))
            return found, fetcher.stopped_hosts(), dict(fetcher.stats)

    results, stopped, http_stats = asyncio.run(go())
    cache.save()
    table = Table("source", "listing", "tabs", "section", "rss", "sitemap", "feeds / sitemaps used", "status")
    for r in sorted(results, key=lambda r: (r.ok, r.source_id)):
        eps = r.endpoints or {}
        used = "\n".join((eps.get("feeds") or []) + (eps.get("sitemaps") or [])) or "-"
        status = "[green]ok[/]" if r.ok else f"[red]{r.error or 'nothing found'}[/]"
        tabs = f"{len(eps.get('sections') or [])} / {r.counts.get('section_pages', 0)} pages"
        table.add_row(r.source_id, str(r.counts.get("listing", 0)), tabs, str(r.counts.get("section", 0)),
                      str(r.counts.get("rss", 0)), str(r.counts.get("sitemap", 0)), used, status)
    console.print(table)
    if verbose:
        for r in sorted(results, key=lambda r: r.source_id):
            console.print(f"[bold]{r.source_id}[/] tabs: {' '.join((r.endpoints or {}).get('sections') or []) or '-'}")
    console.print(f"{sum(r.ok for r in results)}/{len(results)} sources returned items")
    blocks = {k: v for k, v in http_stats.items() if k.startswith("block_") or k == "host_blocked"}
    if blocks:
        console.print(f"[yellow]Block answers: {blocks}[/]")
    for host, why in stopped.items():
        console.print(f"[red]Not fetched: {host}: {why}[/]")
    console.print(f"Block log: {block_log.path}")
    imported = asyncio.run(_sync_block_logs(cfg, quiet=True))
    if imported:
        console.print(f"Block logs imported into Postgres: {', '.join(imported)}")


@app.command()
def export(
    day: Optional[str] = typer.Option(None, help="Also write daily CSV for this date (YYYY-MM-DD); default today"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Rebuild CSV / Excel exports from the database."""
    cfg = load_config(config)
    the_day = date.fromisoformat(day) if day else datetime.now(ZoneInfo(cfg.timezone)).date()

    async def go():
        storage = await _storage(cfg)
        try:
            return await export_all(storage, cfg.data_dir / "exports", Classifier(cfg.keywords).labels,
                                    cfg.timezone, day=the_day)
        finally:
            await storage.close()

    counts = asyncio.run(go())
    console.print(f"Exported to {cfg.data_dir / 'exports'}: {counts}")


@app.command("reclassify")
def reclassify_cmd(config: Optional[Path] = ConfigOpt) -> None:
    """Re-apply keywords.yaml to stored articles, then rebuild exports."""
    cfg = load_config(config)
    try:
        with run_lock(cfg.data_dir / ".run.lock"):
            counts = asyncio.run(reclassify(cfg))
    except RunInProgress as exc:
        console.print(f"[yellow]A scrape is running ({exc}); try again when it finishes.[/]")
        raise typer.Exit(1)
    console.print(dict(counts))
    export(day=None, config=config)


@app.command()
def stats(config: Optional[Path] = ConfigOpt) -> None:
    """Article counts per category and per source."""
    cfg = load_config(config)

    async def go():
        storage = await _storage(cfg)
        try:
            if (await storage.query_df("SELECT COUNT(*) AS n FROM articles"))["n"][0] == 0:
                console.print("No articles saved yet. Run `avn run` first.")
                return
            console.print((await storage.query_df(
                "SELECT category, COUNT(*) AS articles, MIN(published_date) AS first, MAX(published_date) AS last "
                "FROM v_matches GROUP BY category ORDER BY articles DESC")).to_string(index=False))
            console.print()
            console.print((await storage.query_df(
                "SELECT source_name, COUNT(DISTINCT article_id) AS articles FROM v_matches "
                "GROUP BY source_name ORDER BY articles DESC LIMIT 30")).to_string(index=False))
        finally:
            await storage.close()

    asyncio.run(go())


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Address to listen on"),
    port: int = typer.Option(8100, help="Port to listen on"),
    no_schedule: bool = typer.Option(False, "--no-schedule", help="Serve the API without the daily scrape"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the service: news API, review desk API, and the daily scrape."""
    import uvicorn  # only the service needs it

    from .main import create_app

    _setup_logging(verbose)
    try:
        api = create_app(schedule=not no_schedule)
    except ValidationError as exc:
        names = ", ".join(sorted({str(e["loc"][0]).upper() for e in exc.errors()}))
        console.print(f"[red]Missing or invalid in .env: {names}. See .env.example.[/]")
        raise typer.Exit(1)
    except SettingsError as exc:
        console.print(f"[red]{exc}. See .env.example.[/]")
        raise typer.Exit(1)
    uvicorn.run(api, host=host, port=port, log_level="info")


@app.command()
def migrate() -> None:
    """Create or update the database tables."""

    async def go() -> list[str]:
        conn = await connect()
        try:
            return await apply_migrations(conn)
        finally:
            await conn.close()

    try:
        applied = asyncio.run(go())
    except DatabaseUnavailable as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"Applied: {', '.join(applied)}" if applied else "Nothing to apply.")


@app.command("import-sqlite")
def import_sqlite_cmd(
    sqlite: Optional[Path] = typer.Option(None, help="Old database (default: data/db/news.sqlite)"),
    accepted: Optional[Path] = typer.Option(None, help="The site's accepted.json, if any"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Once: copy the old SQLite data and accepted stories into Postgres."""
    cfg = load_config(config)
    path = sqlite or cfg.data_dir / "db" / "news.sqlite"
    if not path.exists():
        console.print(f"[red]{path} does not exist.[/]")
        raise typer.Exit(1)

    async def go():
        storage = await _storage(cfg)
        try:
            return await import_sqlite(storage, path, accepted)
        finally:
            await storage.close()

    counts = asyncio.run(go())
    console.print(f"New rows: {counts}")


if __name__ == "__main__":
    app()
