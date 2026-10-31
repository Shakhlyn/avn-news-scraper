import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from avn_scraper.scraper.config import Config
from avn_scraper.scraper.importer import import_sqlite
from avn_scraper.scraper.pipeline import RunOptions, _run, lookback_hours, run_pipeline
from avn_scraper.scraper.storage import DatabaseUnavailable, PostgresStorage, connect, migrate

NOW = datetime(2026, 10, 30, 1, 0, tzinfo=timezone.utc)


def record(rid, **over):
    return {"id": rid, "url": f"https://x.test/{rid}", "canonical_url": f"https://x.test/{rid}",
            "source_id": "x", "source_name": "X", "source_type": "national", "language": "bn",
            "title": f"title {rid}", "summary": "s", "body": "b", "author": None, "image": None,
            "published_at": NOW.isoformat(), "published_date": "2026-10-30", "scraped_at": NOW.isoformat(),
            "discovered_via": "rss", "district": None, "districts_mentioned": ["Dhaka", "Khulna"],
            "story_cluster_id": rid, "body_missing": False, "run_id": "r1",
            "categories": [{"category": "death_penalty", "score": 10, "matched_terms": ["ফাঁসি"]}], **over}


@pytest.fixture
def storage(tmp_path, database):
    s = PostgresStorage(tmp_path)
    yield s
    s.close()


def test_migrate_applies_once(database):
    conn = connect(database)
    assert migrate(conn) == []
    conn.close()


def test_saving_twice_updates_one_row_and_its_categories(storage):
    storage.save_article(record("a"), None)
    storage.save_article(record("a", title="new title",
                                categories=[{"category": "border_killing", "score": 5, "matched_terms": []}]), None)
    rows = storage.conn.execute("SELECT title, districts_mentioned FROM articles").fetchall()
    assert rows == [{"title": "new title", "districts_mentioned": ["Dhaka", "Khulna"]}]
    cats = storage.conn.execute("SELECT category FROM article_categories").fetchall()
    assert cats == [{"category": "border_killing"}]


def test_seen_urls_dedupe_and_known_ids(storage):
    storage.save_article(record("a"), None)
    storage.mark_seen([("b", "https://x.test/b", "no_match"), ("b", "https://x.test/b", "old")])
    assert storage.known_ids(["a", "b", "c"]) == {"a", "b"}
    assert storage.conn.execute("SELECT status FROM seen_urls").fetchall() == [{"status": "no_match"}]


def test_nul_characters_are_dropped(storage):
    storage.save_article(record("a", title="be\x00fore", body="b\x00"), None)
    row = storage.conn.execute("SELECT title, body FROM articles").fetchone()
    assert row == {"title": "before", "body": "b"}


def test_a_failed_article_leaves_nothing_behind(storage):
    bad = record("a", categories=[{"category": None, "score": 1, "matched_terms": []}])
    with pytest.raises(psycopg.Error):
        storage.save_article(bad, None)
    assert storage.conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 0


def test_a_bad_row_is_counted_and_the_run_goes_on(tmp_path, database, monkeypatch):
    calls = []
    real = PostgresStorage.save_article

    def save(self, rec, raw):
        calls.append(rec["id"])
        if len(calls) == 1:
            raise psycopg.DataError("bad value")
        real(self, rec, raw)

    monkeypatch.setattr(PostgresStorage, "save_article", save)
    import test_pipeline as tp
    config = tp.make_config(tmp_path, load_keywords())
    result = asyncio.run(run_pipeline(config, RunOptions(), transport=tp.make_site(tp.Counter())))
    assert result.stats["outcome"]["db_failed"] == 1 and result.stats["outcome"]["saved"] == 1


def load_keywords():
    from avn_scraper.scraper.config import load_config
    return load_config().keywords


def test_a_lost_connection_stops_the_run(tmp_path, database, monkeypatch):
    def save(self, rec, raw):
        raise psycopg.OperationalError("server closed the connection")

    monkeypatch.setattr(PostgresStorage, "save_article", save)
    import test_pipeline as tp
    config = tp.make_config(tmp_path, load_keywords())
    with pytest.raises(psycopg.OperationalError):
        asyncio.run(run_pipeline(config, RunOptions(), transport=tp.make_site(tp.Counter())))
    assert not (tmp_path / "cache" / "listing_seen.json").exists()


def test_database_down_fails_before_any_web_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setattr("avn_scraper.scraper.storage.postgres.time.sleep", lambda _: None)
    config = Config(settings={}, sources=[], keywords={}, config_dir=tmp_path)
    with pytest.raises(DatabaseUnavailable, match="Cannot reach Postgres after 3 tries"):
        asyncio.run(run_pipeline(config, RunOptions(), transport=None))


def test_missing_tables_say_to_migrate(tmp_path, database):
    conn = connect(database)
    conn.execute("DROP TABLE articles CASCADE")
    with pytest.raises(DatabaseUnavailable, match="avn migrate"):
        PostgresStorage(tmp_path, conn)


def test_dry_run_writes_nothing(tmp_path, cfg, database):
    import test_pipeline as tp
    config = tp.make_config(tmp_path, cfg.keywords)
    result = asyncio.run(run_pipeline(config, RunOptions(dry_run=True), transport=tp.make_site(tp.Counter())))
    assert result.saved
    conn = connect(database)
    for table in ("sources", "articles", "seen_urls", "runs"):
        assert conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0
    conn.close()


@pytest.mark.parametrize("gap,expected", [(None, 24), (timedelta(hours=3), 24), (timedelta(days=3), 72)])
def test_lookback_reaches_the_last_run_and_never_less_than_the_window(gap, expected):
    assert lookback_hours(24, NOW - gap if gap else None, NOW) == expected


def test_last_full_run_skips_partial_runs(storage):
    assert storage.last_full_run() is None
    storage.save_run("full", NOW.isoformat(), NOW.isoformat(), {})
    later = (NOW + timedelta(hours=5)).isoformat()
    storage.save_run("part", later, later, {"only_sources": ["x"]})
    assert storage.last_full_run() == NOW


def test_since_last_run_widens_the_cutoff(tmp_path, cfg, storage):
    import test_pipeline as tp
    start = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    storage.save_run("old", start, start, {})
    config = tp.make_config(tmp_path, cfg.keywords)
    result = asyncio.run(_run(config, RunOptions(since_last_run=True), storage, tp.make_site(tp.Counter())))
    cutoff = datetime.fromisoformat(result.stats["cutoff"])
    assert timedelta(hours=71) < datetime.now(timezone.utc) - cutoff < timedelta(hours=73)


def make_sqlite(path):
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE sources (id TEXT PRIMARY KEY, name TEXT, language TEXT, type TEXT, district TEXT, homepage TEXT);
    CREATE TABLE articles (id TEXT PRIMARY KEY, url TEXT, published_at TEXT, published_date TEXT, scraped_at TEXT,
                           districts_mentioned TEXT, body_missing INTEGER, json_path TEXT);
    CREATE TABLE article_categories (article_id TEXT, category TEXT, score INTEGER, matched_terms TEXT);
    CREATE TABLE review (article_id TEXT, verified INTEGER, notes TEXT, reviewed_at TEXT);
    CREATE TABLE seen_urls (id TEXT, url TEXT, status TEXT, first_seen TEXT);
    CREATE TABLE runs (run_id TEXT, started_at TEXT, finished_at TEXT, stats_json TEXT);
    INSERT INTO sources VALUES ('x', 'X', 'bn', 'national', NULL, 'https://x.test');
    INSERT INTO articles VALUES ('a', 'https://x.test/a', '', '2026-10-30', '2026-10-30T01:00:00+00:00', 'Dhaka,Khulna', 1, 'p.json');
    INSERT INTO article_categories VALUES ('a', 'death_penalty', 9, '["ফাঁসি"]');
    INSERT INTO seen_urls VALUES ('b', 'https://x.test/b', 'old', '2026-10-30T01:00:00+00:00');
    INSERT INTO runs VALUES ('r', '2026-10-30T01:00:00+00:00', '2026-10-30T01:10:00+00:00', '{"outcome": {}}');
    """)
    db.commit()
    db.close()


def test_import_copies_everything_and_can_run_again(tmp_path, storage):
    path = tmp_path / "news.sqlite"
    make_sqlite(path)
    accepted = tmp_path / "accepted.json"
    accepted.write_text(json.dumps([{"id": "s", "story": {"id": "s"}, "at": "2026-10-29T08:30:48.800Z", "by": ""}]))

    first = import_sqlite(storage, path, accepted)
    assert first == {"sources": 1, "articles": 1, "article_categories": 1, "review": 0,
                     "seen_urls": 1, "runs": 1, "accepted": 1}
    row = storage.conn.execute("SELECT published_at, districts_mentioned, body_missing FROM articles").fetchone()
    assert row == {"published_at": None, "districts_mentioned": ["Dhaka", "Khulna"], "body_missing": True}
    assert set(import_sqlite(storage, path, accepted).values()) == {0}
