"""Local filesystem + SQLite storage.

data/
  db/news.sqlite
  articles/YYYY/MM/DD/<source>/<id>.json
  raw/YYYY/MM/DD/<source>/<id>.html.gz
"""

from __future__ import annotations

import gzip
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from ..classify import Match
from ..config import Source
from .base import Storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, name TEXT, language TEXT, type TEXT, district TEXT, homepage TEXT
);
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    canonical_url TEXT,
    source_id TEXT,
    source_name TEXT,
    source_type TEXT,
    language TEXT,
    title TEXT,
    summary TEXT,
    body TEXT,
    author TEXT,
    image TEXT,
    published_at TEXT,          -- ISO 8601, UTC
    published_date TEXT,        -- YYYY-MM-DD in Asia/Dhaka (falls back to scrape date)
    scraped_at TEXT,
    discovered_via TEXT,        -- rss | sitemap | listing | catchall
    district TEXT,              -- best guess
    districts_mentioned TEXT,   -- comma separated
    story_cluster_id TEXT,      -- same story across outlets share this id
    body_missing INTEGER DEFAULT 0,
    json_path TEXT,
    raw_path TEXT,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_date ON articles(published_date);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
CREATE INDEX IF NOT EXISTS idx_articles_cluster ON articles(story_cluster_id);

CREATE TABLE IF NOT EXISTS article_categories (
    article_id TEXT REFERENCES articles(id) ON DELETE CASCADE,
    category TEXT,
    score INTEGER,
    matched_terms TEXT,         -- JSON list
    PRIMARY KEY (article_id, category)
);
CREATE INDEX IF NOT EXISTS idx_cat_category ON article_categories(category);

-- Manual review: verified = 1 relevant, 0 false positive, NULL not reviewed.
CREATE TABLE IF NOT EXISTS review (
    article_id TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    verified INTEGER,
    notes TEXT,
    reviewed_at TEXT
);

-- URLs fetched but not stored (no match / too old), so they are not re-fetched.
CREATE TABLE IF NOT EXISTS seen_urls (
    id TEXT PRIMARY KEY, url TEXT, status TEXT, first_seen TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, stats_json TEXT
);

-- Full-text search (trigram works for Bangla and English substrings):
--   SELECT a.title FROM articles_fts f JOIN articles a ON a.rowid = f.rowid
--   WHERE articles_fts MATCH '"আয়নাঘর"';
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, body, content='articles', content_rowid='rowid', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, body) VALUES ('delete', old.rowid, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, body) VALUES ('delete', old.rowid, old.title, old.body);
    INSERT INTO articles_fts(rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;

-- One row per (article, category): the main view for analysis.
CREATE VIEW IF NOT EXISTS v_matches AS
SELECT a.published_date, c.category, a.source_name, a.source_type, a.language,
       a.title, a.url, a.district, c.score, c.matched_terms, a.story_cluster_id,
       a.published_at, a.scraped_at, a.id AS article_id, a.json_path, r.verified, r.notes
FROM article_categories c
JOIN articles a ON a.id = c.article_id
LEFT JOIN review r ON r.article_id = a.id;
"""

_ARTICLE_COLUMNS = (
    "id", "url", "canonical_url", "source_id", "source_name", "source_type", "language",
    "title", "summary", "body", "author", "image", "published_at", "published_date",
    "scraped_at", "discovered_via", "district", "districts_mentioned", "story_cluster_id",
    "body_missing", "json_path", "raw_path", "run_id",
)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


class LocalStorage(Storage):
    def __init__(self, data_dir: Path):
        self.root = data_dir
        (self.root / "db").mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "db" / "news.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- paths -------------------------------------------------------------
    def _rel_dir(self, kind: str, record: dict[str, Any]) -> Path:
        y, m, d = record["published_date"].split("-")
        return Path(kind) / y / m / d / _safe(record["source_id"])

    # -- Storage API ---------------------------------------------------------
    def upsert_sources(self, sources: Iterable[Source]) -> None:
        self.conn.executemany(
            "INSERT INTO sources(id, name, language, type, district, homepage) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, language=excluded.language, "
            "type=excluded.type, district=excluded.district, homepage=excluded.homepage",
            [(s.id, s.name, s.language, s.type, s.district, s.homepage) for s in sources],
        )
        self.conn.commit()

    def known_ids(self, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        found: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for table in ("articles", "seen_urls"):
                rows = self.conn.execute(f"SELECT id FROM {table} WHERE id IN ({marks})", chunk)
                found.update(r[0] for r in rows)
        return found

    def mark_seen(self, rows: Iterable[tuple[str, str, str]]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_urls(id, url, status, first_seen) VALUES (?,?,?,?)",
            [(i, u, s, now) for i, u, s in rows],
        )
        self.conn.commit()

    def recent_titles(self, days: int) -> list[tuple[str, str, str]]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT id, story_cluster_id, title FROM articles WHERE scraped_at >= ?", (since,))
        return [(r[0], r[1], r[2]) for r in rows]

    def save_article(self, record: dict[str, Any], raw_html: str | None) -> None:
        record = dict(record)
        rel_dir = self._rel_dir("articles", record)
        json_rel = rel_dir / f"{record['id']}.json"
        record["json_path"] = str(json_rel)
        if raw_html:
            raw_rel = self._rel_dir("raw", record) / f"{record['id']}.html.gz"
            (self.root / raw_rel).parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(self.root / raw_rel, "wt", encoding="utf-8") as fh:
                fh.write(raw_html)
            record["raw_path"] = str(raw_rel)
        else:
            record["raw_path"] = None

        (self.root / json_rel).parent.mkdir(parents=True, exist_ok=True)
        (self.root / json_rel).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        row = dict(record)
        row["districts_mentioned"] = ",".join(record.get("districts_mentioned") or [])
        row["body_missing"] = int(bool(record.get("body_missing")))
        cols = ",".join(_ARTICLE_COLUMNS)
        marks = ",".join("?" * len(_ARTICLE_COLUMNS))
        # Upsert (not INSERT OR REPLACE, which would cascade-delete review rows).
        updates = ",".join(f"{c}=excluded.{c}" for c in _ARTICLE_COLUMNS if c != "id")
        self.conn.execute(f"INSERT INTO articles({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
                          [row.get(c) for c in _ARTICLE_COLUMNS])
        self._write_categories(record["id"], record.get("categories", []))
        self.conn.commit()

    def _write_categories(self, article_id: str, categories: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM article_categories WHERE article_id = ?", (article_id,))
        self.conn.executemany(
            "INSERT INTO article_categories(article_id, category, score, matched_terms) VALUES (?,?,?,?)",
            [(article_id, c["category"], c["score"], json.dumps(c["matched_terms"], ensure_ascii=False))
             for c in categories],
        )

    def replace_categories(self, article_id: str, matches: list[Match]) -> None:
        categories = [{"category": m.category, "score": m.score, "matched_terms": m.matched_terms}
                      for m in matches]
        self._write_categories(article_id, categories)
        row = self.conn.execute("SELECT json_path FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row and row[0] and (self.root / row[0]).exists():
            path = self.root / row[0]
            record = json.loads(path.read_text(encoding="utf-8"))
            record["categories"] = categories
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        self.conn.commit()

    def iter_articles(self) -> Iterator[dict[str, Any]]:
        for row in self.conn.execute("SELECT * FROM articles"):
            yield dict(row)

    def save_run(self, run_id: str, started_at: str, finished_at: str, stats: dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                          (run_id, started_at, finished_at, json.dumps(stats, ensure_ascii=False)))
        self.conn.commit()

    def query_df(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn, params=params)
