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
from rich.console import Console
from rich.table import Table

from .scraper.classify import Classifier
from .scraper.config import load_config
from .scraper.discover.sources import EndpointCache, ListingMemory, discover_source
from .scraper.export import export_all
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


def _storage(cfg) -> PostgresStorage:
    try:
        return PostgresStorage(cfg.data_dir)
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
    if result.saved:
        title = "Would save (dry run)" if dry_run else "Saved to Postgres"
        table = Table("date", "source", "categories", "title", title=title, show_lines=False)
        for r in result.saved:
            table.add_row(r["published_date"], r["source_name"],
                          ", ".join(f"{c['category']}({c['score']})" for c in r["categories"]),
                          r["title"][:90])
        console.print(table)


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

    async def go():
        async with Fetcher(cfg.settings.get("http") or {}) as fetcher:
            return await asyncio.gather(*(discover_source(fetcher, s, cutoff, cache, cfg.settings.get("sections"), memory)
                                          for s in selected))

    results = asyncio.run(go())
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


@app.command()
def export(
    day: Optional[str] = typer.Option(None, help="Also write daily CSV for this date (YYYY-MM-DD); default today"),
    config: Optional[Path] = ConfigOpt,
) -> None:
    """Rebuild CSV / Excel exports from the database."""
    cfg = load_config(config)
    storage = _storage(cfg)
    the_day = date.fromisoformat(day) if day else datetime.now(ZoneInfo(cfg.timezone)).date()
    counts = export_all(storage, cfg.data_dir / "exports", Classifier(cfg.keywords).labels, cfg.timezone, day=the_day)
    storage.close()
    console.print(f"Exported to {cfg.data_dir / 'exports'}: {counts}")


@app.command("reclassify")
def reclassify_cmd(config: Optional[Path] = ConfigOpt) -> None:
    """Re-apply keywords.yaml to stored articles, then rebuild exports."""
    cfg = load_config(config)
    try:
        with run_lock(cfg.data_dir / ".run.lock"):
            counts = reclassify(cfg)
    except RunInProgress as exc:
        console.print(f"[yellow]A scrape is running ({exc}); try again when it finishes.[/]")
        raise typer.Exit(1)
    console.print(dict(counts))
    export(day=None, config=config)


@app.command()
def stats(config: Optional[Path] = ConfigOpt) -> None:
    """Article counts per category and per source."""
    cfg = load_config(config)
    storage = _storage(cfg)
    if storage.query_df("SELECT COUNT(*) AS n FROM articles")["n"][0] == 0:
        console.print("No articles saved yet. Run `avn run` first.")
        storage.close()
        return
    console.print(storage.query_df(
        "SELECT category, COUNT(*) AS articles, MIN(published_date) AS first, MAX(published_date) AS last "
        "FROM v_matches GROUP BY category ORDER BY articles DESC").to_string(index=False))
    console.print()
    console.print(storage.query_df(
        "SELECT source_name, COUNT(DISTINCT article_id) AS articles FROM v_matches "
        "GROUP BY source_name ORDER BY articles DESC LIMIT 30").to_string(index=False))
    storage.close()


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
    except ValidationError:
        console.print("[red]DATABASE_URL is not set. Copy .env.example to .env and fill it in.[/]")
        raise typer.Exit(1)
    uvicorn.run(api, host=host, port=port, log_level="info")


@app.command()
def migrate() -> None:
    """Create or update the database tables."""
    try:
        conn = connect()
    except DatabaseUnavailable as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    applied = apply_migrations(conn)
    conn.close()
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
    storage = _storage(cfg)
    counts = import_sqlite(storage, path, accepted)
    storage.close()
    console.print(f"New rows: {counts}")


if __name__ == "__main__":
    app()
