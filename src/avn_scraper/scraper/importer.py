"""One-off move of the old local data into Postgres: the SQLite database and the
site's accepted.json. All of it or none of it (one transaction), and safe to run
again: rows already in Postgres are left as they are. SQLite is only read."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from psycopg.types.json import Jsonb

from .storage import PostgresStorage
from .storage.postgres import clean


def _rows(db: sqlite3.Connection, table: str) -> list[dict]:
    return [{k: clean(v) for k, v in dict(r).items()} for r in db.execute(f"SELECT * FROM {table}")]


def _insert(storage: PostgresStorage, table: str, rows: list[dict]) -> int:
    """Insert `rows` and return how many were new."""
    if not rows:
        return 0
    cols = list(rows[0])
    sql = (f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join(f'%({c})s' for c in cols)}) "
           "ON CONFLICT DO NOTHING")
    with storage.conn.cursor() as cur:
        cur.executemany(sql, rows)
        return cur.rowcount


def import_sqlite(storage: PostgresStorage, sqlite_path: Path, accepted_path: Path | None = None) -> dict[str, int]:
    db = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    articles = _rows(db, "articles")
    for a in articles:
        a.pop("json_path", None)
        a.pop("rowid", None)
        a["districts_mentioned"] = [d for d in (a["districts_mentioned"] or "").split(",") if d]
        a["body_missing"] = bool(a["body_missing"])
        a["published_at"] = a["published_at"] or None
    categories = _rows(db, "article_categories")
    for c in categories:
        c["matched_terms"] = Jsonb(json.loads(c["matched_terms"] or "[]"))
    runs = _rows(db, "runs")
    for r in runs:
        r["stats"] = Jsonb(json.loads(r.pop("stats_json") or "{}"))
    review = _rows(db, "review")
    seen = _rows(db, "seen_urls")
    sources = _rows(db, "sources")
    db.close()

    accepted = []
    if accepted_path:
        accepted = [{"id": a["id"], "story": Jsonb(a["story"]), "accepted_at": a["at"], "accepted_by": a.get("by") or ""}
                    for a in json.loads(accepted_path.read_text(encoding="utf-8"))]

    with storage.conn.transaction():
        return {
            "sources": _insert(storage, "sources", sources),
            "articles": _insert(storage, "articles", articles),
            "article_categories": _insert(storage, "article_categories", categories),
            "review": _insert(storage, "review", review),
            "seen_urls": _insert(storage, "seen_urls", seen),
            "runs": _insert(storage, "runs", runs),
            "accepted": _insert(storage, "accepted", accepted),
        }
