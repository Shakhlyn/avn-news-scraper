"""Postgres storage. The database holds everything but the raw pages:

data/raw/YYYY/MM/DD/<source>/<id>.html.gz   (.json.gz for sites read through an API adapter)

The connection comes from DATABASE_URL, set in the environment or in .env.
"""

from __future__ import annotations

import asyncio
import gzip
import os
import re
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ...core.database import fetch_all, fetch_one
from ..classify import Match
from ..config import PROJECT_ROOT, Source
from .base import Storage

_ARTICLE_COLUMNS = (
    "id", "url", "canonical_url", "source_id", "source_name", "source_type", "language",
    "title", "summary", "body", "author", "image", "published_at", "published_date",
    "scraped_at", "discovered_via", "district", "districts_mentioned", "story_cluster_id",
    "body_missing", "raw_path", "run_id",
)


class DatabaseUnavailable(Exception):
    """Postgres cannot be used; the message says what to do."""


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def clean(value: Any) -> Any:
    """Postgres text cannot hold NUL characters, and scraped pages sometimes do."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def database_url() -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise DatabaseUnavailable("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return url


async def connect(url: str | None = None, attempts: int = 3, delay: float = 2.0) -> psycopg.AsyncConnection:
    url = url or database_url()
    for attempt in range(1, attempts + 1):
        try:
            return await psycopg.AsyncConnection.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=5)
        except psycopg.OperationalError as exc:
            if attempt == attempts:
                raise DatabaseUnavailable(
                    f"Cannot reach Postgres after {attempts} tries. "
                    "Start it with ./scrape-now.sh or `docker start avn-postgres`.") from exc
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def migrate(conn: psycopg.AsyncConnection) -> list[str]:
    """Apply the migrations not applied yet, each in one transaction. Returns their names."""
    await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                       "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    done = {r["filename"] for r in await fetch_all(conn, "SELECT filename FROM schema_migrations")}
    folder = resources.files("avn_scraper") / "migrations"
    files = sorted((f for f in folder.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    applied = []
    for file in files:
        if file.name in done:
            continue
        async with conn.transaction():
            await conn.execute(file.read_text(encoding="utf-8").encode("utf-8"))
            await conn.execute("INSERT INTO schema_migrations(filename) VALUES (%s)", (file.name,))
        applied.append(file.name)
    return applied


class PostgresStorage(Storage):
    def __init__(self, data_dir: Path, conn: psycopg.AsyncConnection):
        self.root = data_dir
        self.conn = conn

    @classmethod
    async def open(cls, data_dir: Path, conn: psycopg.AsyncConnection | None = None) -> PostgresStorage:
        conn = conn or await connect()
        row = await fetch_one(conn, "SELECT to_regclass('articles') IS NOT NULL AS ready")
        if not row["ready"]:
            await conn.close()
            raise DatabaseUnavailable("The tables do not exist yet. Run `avn migrate` first.")
        return cls(data_dir, conn)

    async def close(self) -> None:
        await self.conn.close()

    async def upsert_sources(self, sources: Iterable[Source]) -> None:
        async with self.conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO sources(id, name, language, type, district, homepage) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, language=excluded.language, "
                "type=excluded.type, district=excluded.district, homepage=excluded.homepage",
                [(s.id, s.name, s.language, s.type, s.district, s.homepage) for s in sources],
            )

    async def known_ids(self, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        rows = await fetch_all(
            self.conn,
            "SELECT id FROM articles WHERE id = ANY(%(ids)s) UNION SELECT id FROM seen_urls WHERE id = ANY(%(ids)s)",
            {"ids": ids})
        return {r["id"] for r in rows}

    async def mark_seen(self, rows: Iterable[tuple[str, str, str]]) -> None:
        now = datetime.now(timezone.utc)
        async with self.conn.transaction(), self.conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO seen_urls(id, url, status, first_seen) VALUES (%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                [(i, clean(u), clean(s), now) for i, u, s in rows],
            )

    async def recent_titles(self, days: int) -> list[tuple[str, str, str]]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = await fetch_all(
            self.conn, "SELECT id, story_cluster_id, title FROM articles WHERE scraped_at >= %s", (since,))
        return [(r["id"], r["story_cluster_id"], r["title"]) for r in rows]

    def _write_raw(self, record: dict[str, Any], raw_html: str | None) -> str | None:
        if not raw_html:
            return None
        y, m, d = record["published_date"].split("-")
        ext = "json" if raw_html.lstrip().startswith(("{", "[")) else "html"
        rel = Path("raw") / y / m / d / _safe(record["source_id"]) / f"{record['id']}.{ext}.gz"
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(self.root / rel, "wt", encoding="utf-8") as fh:
            fh.write(raw_html)
        return str(rel)

    async def save_article(self, record: dict[str, Any], raw_html: str | None) -> None:
        row = {c: clean(record.get(c)) for c in _ARTICLE_COLUMNS}
        row["raw_path"] = await asyncio.to_thread(self._write_raw, record, raw_html)
        row["districts_mentioned"] = row["districts_mentioned"] or []
        row["body_missing"] = bool(row["body_missing"])
        cols = ",".join(_ARTICLE_COLUMNS)
        marks = ",".join(f"%({c})s" for c in _ARTICLE_COLUMNS)
        updates = ",".join(f"{c}=excluded.{c}" for c in _ARTICLE_COLUMNS if c != "id")
        async with self.conn.transaction():
            await self.conn.execute(f"INSERT INTO articles({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}", row)
            await self._write_categories(record["id"], record.get("categories", []))

    async def _write_categories(self, article_id: str, categories: list[dict[str, Any]]) -> None:
        await self.conn.execute("DELETE FROM article_categories WHERE article_id = %s", (article_id,))
        async with self.conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO article_categories(article_id, category, score, matched_terms) VALUES (%s,%s,%s,%s)",
                [(article_id, c["category"], c["score"], Jsonb(clean(c["matched_terms"]))) for c in categories],
            )

    async def replace_categories(self, article_id: str, matches: list[Match]) -> None:
        categories = [{"category": m.category, "score": m.score, "matched_terms": m.matched_terms}
                      for m in matches]
        async with self.conn.transaction():
            await self._write_categories(article_id, categories)

    async def iter_articles(self) -> AsyncIterator[dict[str, Any]]:
        for row in await fetch_all(self.conn, "SELECT * FROM articles"):
            yield row

    async def save_run(self, run_id: str, started_at: str, finished_at: str, stats: dict[str, Any]) -> None:
        await self.conn.execute(
            "INSERT INTO runs(run_id, started_at, finished_at, stats) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT(run_id) DO UPDATE SET started_at=excluded.started_at, "
            "finished_at=excluded.finished_at, stats=excluded.stats",
            (run_id, started_at, finished_at, Jsonb(stats)))

    async def last_full_run(self) -> datetime | None:
        """When the last finished run over all outlets started."""
        row = await fetch_one(self.conn, "SELECT max(started_at) AS at FROM runs WHERE finished_at IS NOT NULL "
                                         "AND stats->'only_sources' IS NULL")
        return row["at"]

    async def query_df(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        async with self.conn.cursor() as cur:
            await cur.execute(sql, params or None)
            columns = [c.name for c in cur.description]
            rows = await cur.fetchall()
        return await asyncio.to_thread(pd.DataFrame, rows, columns=columns)
