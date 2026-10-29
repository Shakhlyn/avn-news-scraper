import json
from datetime import datetime, timezone

import pytest

from avn_scraper.publish import build_news, safe_url, snippet, to_json, upload_r2
from avn_scraper.storage import LocalStorage

NOW = datetime(2026, 10, 29, 1, 30, tzinfo=timezone.utc)
ORDER = ["extrajudicial_killing", "border_killing", "enforced_disappearance", "death_penalty"]


def record(id, scraped_at="2026-10-29T01:05:00+00:00", source="dailystar", cluster=None,
           cats=(("extrajudicial_killing", 10),), **kw):
    rec = {
        "id": id, "url": f"https://example.com/{id}", "canonical_url": None,
        "source_id": source, "source_name": source.title(), "source_type": "national",
        "language": "bn", "title": f"  Title   {id} ", "summary": "", "body": "word " * 200,
        "author": "", "image": "https://cdn.example.com/x.jpg",
        "published_at": "2026-10-29T00:30:00+00:00", "published_date": "2026-10-29",
        "scraped_at": scraped_at, "discovered_via": "rss", "district": "Dhaka",
        "districts_mentioned": ["Dhaka"], "story_cluster_id": cluster or id, "body_missing": False,
        "run_id": "r1",
        "categories": [{"category": c, "score": s, "matched_terms": ["ক্রসফায়ার"]} for c, s in cats],
    }
    rec.update(kw)
    return rec


@pytest.fixture
def storage(tmp_path):
    s = LocalStorage(tmp_path)
    yield s
    s.close()


def test_snippet_cuts_at_a_word():
    assert snippet("  a   b  ") == "a b"
    cut = snippet("word " * 200)
    assert cut.endswith("…") and len(cut) <= 321 and not cut.endswith(" …")


def test_only_the_window_is_published_and_bodies_stay_out(storage):
    storage.save_article(record("new"), None)
    storage.save_article(record("old", scraped_at="2026-10-28T01:00:00+00:00"), None)
    news = build_news(storage, ORDER, hours=24, now=NOW)
    assert news["from"] == "2026-10-28T01:30:00+00:00" and news["to"] == "2026-10-29T01:30:00+00:00"
    [story] = news["stories"]
    [report] = story["reports"]
    assert report["id"] == "new" and report["title"] == "Title new"
    assert report["url"] == "https://example.com/new"
    assert "body" not in report and "image" not in report
    assert report["summary"].endswith("…")
    assert report["categories"] == [{"category": "extrajudicial_killing", "score": 10, "terms": ["ক্রসফায়ার"]}]


def test_a_cluster_is_one_story_and_the_widest_reported_leads(storage):
    storage.save_article(record("solo", cats=(("death_penalty", 30),)), None)
    storage.save_article(record("x1", cluster="x"), None)
    storage.save_article(record("x2", cluster="x", source="prothomalo", published_at="2026-10-29T01:00:00+00:00",
                                cats=(("enforced_disappearance", 4),)), None)
    stories = build_news(storage, ORDER, now=NOW)["stories"]
    assert [s["id"] for s in stories] == ["x", "solo"]
    assert [r["id"] for r in stories[0]["reports"]] == ["x2", "x1"]
    assert stories[0]["categories"] == ["extrajudicial_killing", "enforced_disappearance"]


def test_false_positives_marked_in_review_are_left_out(storage):
    storage.save_article(record("keep"), None)
    storage.save_article(record("wrong"), None)
    storage.conn.execute("INSERT INTO review(article_id, verified) VALUES ('wrong', 0)")
    storage.conn.commit()
    stories = build_news(storage, ORDER, now=NOW)["stories"]
    assert [s["id"] for s in stories] == ["keep"]


def test_an_empty_window_is_an_empty_file(storage):
    news = json.loads(to_json(build_news(storage, ORDER, now=NOW)))
    assert news["stories"] == [] and news["windowHours"] == 24


class FakeS3:
    def __init__(self):
        self.calls = []

    def put_object(self, **kw):
        self.calls.append(kw)


def test_upload_writes_the_staging_file():
    s3 = FakeS3()
    settings = {"R2_ACCOUNT_ID": "a", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s", "R2_BUCKET": "avn-news"}
    assert upload_r2(b"{}", settings, client=s3) == "staging/news.json"
    assert s3.calls == [{"Bucket": "avn-news", "Key": "staging/news.json", "Body": b"{}",
                         "ContentType": "application/json; charset=utf-8"}]


def test_upload_needs_credentials_from_the_environment(monkeypatch):
    for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(RuntimeError, match="R2_BUCKET"):
        upload_r2(b"{}")


def test_only_http_addresses_are_published(storage):
    assert safe_url("https://a.com/x", "https://b.com/y") == "https://a.com/x"
    assert safe_url("javascript:alert(1)", "https://b.com/y") == "https://b.com/y"
    assert safe_url(" JavaScript:alert(1)", "data:text/html,x") == ""
    assert safe_url("//evil.com/x", None) == ""
    storage.save_article(record("a", canonical_url="javascript:alert(1)"), None)
    storage.save_article(record("b", url="javascript:alert(1)"), None)
    urls = {r["id"]: r["url"] for s in build_news(storage, ORDER, now=NOW)["stories"] for r in s["reports"]}
    assert urls == {"a": "https://example.com/a", "b": ""}
