import threading
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from avn_scraper.core.limiter import limiter
from avn_scraper.main import create_app
from avn_scraper.repositories.news import NewsFilters, like_pattern, where_clause
from avn_scraper.services.scheduler import Scheduler, is_due, latest_slot
from avn_scraper.services.scrape_manager import ScrapeManager
from avn_scraper.services.stories import build_stories, safe_url, snippet, to_report
from avn_scraper.scraper.config import Config
from avn_scraper.scraper.pipeline import RunResult
from avn_scraper.scraper.storage import PostgresStorage
from test_postgres import record

DHAKA = ZoneInfo("Asia/Dhaka")
AT_7 = time(7, 0)
NOW = datetime.now(timezone.utc)
ORIGIN = {"origin": "http://testserver"}


async def idle_run(cfg, opts):
    return RunResult("r", {"outcome": {"saved": 0}})


@pytest.fixture
def config(tmp_path):
    return Config(settings={"timezone": "Asia/Dhaka", "data_dir": str(tmp_path)}, sources=[],
                  keywords={"categories": {"border_killing": {}, "death_penalty": {}}}, config_dir=tmp_path)


@pytest.fixture(autouse=True)
def fresh_limits():
    limiter.reset()


def client_for(config, runner=idle_run):
    return TestClient(create_app(config, ScrapeManager(config, runner), schedule=False))


@pytest.fixture
def client(config, database, monkeypatch):
    monkeypatch.setenv("AVN_REVIEW", "on")
    with client_for(config) as c:
        yield c


@pytest.fixture
def readonly(config, database, monkeypatch):
    monkeypatch.setenv("AVN_REVIEW", "")
    with client_for(config) as c:
        yield c


@pytest.fixture
def storage(tmp_path, database):
    s = PostgresStorage(tmp_path)
    yield s
    s.close()


def article(rid, **over):
    return record(rid, **{"scraped_at": NOW.isoformat(), "published_at": NOW.isoformat(), **over})


# stories ---------------------------------------------------------------------
def test_snippet_cuts_at_a_word():
    assert snippet("one  two\nthree") == "one two three"
    assert snippet("alpha beta gamma", 12) == "alpha beta…"
    assert snippet(None) == ""


def test_only_http_addresses_pass():
    assert safe_url("javascript:alert(1)", "https://x.test/a") == "https://x.test/a"
    assert safe_url("data:text/html,x", "") == ""


def test_a_report_has_no_body():
    row = {"id": "a", "url": "https://x.test/a", "canonical_url": None, "source_id": "x", "source_name": None,
           "language": None, "title": " a  b ", "summary": None, "body": "the body", "published_at": None,
           "district": None, "body_missing": 0}
    report = to_report(row, [])
    assert "body" not in report and report["summary"] == "the body" and report["title"] == "a b"


def test_a_cluster_is_one_story_and_the_widest_reported_leads():
    def r(rid, source, score=10, cat="death_penalty", at="2026-10-29"):
        return {"id": rid, "source_id": source, "published_at": at, "district": "",
                "categories": [{"category": cat, "score": score}]}
    clusters = {"solo": "solo", "x1": "x", "x2": "x"}
    reports = [r("solo", "a", 30), r("x1", "a"), r("x2", "b", cat="border_killing", at="2026-10-30")]
    stories = build_stories(reports, lambda x: clusters[x["id"]], ["border_killing", "death_penalty"])
    assert [s["id"] for s in stories] == ["x", "solo"]
    assert [x["id"] for x in stories[0]["reports"]] == ["x2", "x1"]
    assert stories[0]["categories"] == ["border_killing", "death_penalty"]


# query -----------------------------------------------------------------------
def test_dates_or_a_search_reach_past_the_last_24_hours():
    assert "scraped_at" in where_clause(NewsFilters(), NOW)[0]
    assert "scraped_at" not in where_clause(NewsFilters(q="গুম"), NOW)[0]
    assert "scraped_at" not in where_clause(NewsFilters(start=NOW.date()), NOW)[0]


def test_search_wildcards_are_escaped():
    assert like_pattern("50%_a\\") == "%50\\%\\_a\\\\%"


# routes ----------------------------------------------------------------------
def test_news_lists_filters_and_pages(client, storage):
    storage.save_article(article("a", source_id="one", body="বিশেষ শব্দ"), None)
    storage.save_article(article("b", source_id="two"), None)
    storage.save_article(article("old", scraped_at="2020-01-01T00:00:00+00:00"), None)
    assert client.get("/api/v1/news").json()["total"] == 2
    assert client.get("/api/v1/news", params={"q": "বিশেষ"}).json()["total"] == 1
    assert client.get("/api/v1/news", params={"source": "two"}).json()["stories"][0]["id"] == "b"
    assert client.get("/api/v1/news", params={"from": "2020-01-01"}).json()["total"] == 3
    paged = client.get("/api/v1/news", params={"limit": 1, "page": 2}).json()
    assert (paged["pages"], len(paged["stories"])) == (2, 1)
    past_end = client.get("/api/v1/news", params={"page": 9}).json()
    assert (past_end["total"], past_end["stories"]) == (2, [])
    assert "body" not in client.get("/api/v1/news").json()["stories"][0]["reports"][0]


def test_false_positives_are_left_out(client, storage):
    storage.save_article(article("keep"), None)
    storage.save_article(article("wrong"), None)
    storage.conn.execute("INSERT INTO review(article_id, verified) VALUES ('wrong', 0)")
    assert [s["id"] for s in client.get("/api/v1/news").json()["stories"]] == ["keep"]


def test_bad_input_is_a_plain_error(client):
    response = client.get("/api/v1/news", params={"from": "yesterday"})
    assert response.status_code == 400 and "from" in response.json()["error"]


def test_accepting_copies_the_story_from_the_database(client, storage):
    storage.save_article(article("a"), None)
    made = client.post("/api/v1/accepted", json={"id": "a", "story": {"title": "forged"}}, headers=ORIGIN)
    assert made.status_code == 201
    first = made.json()["accepted"]
    assert client.post("/api/v1/accepted", json={"id": "a"}, headers=ORIGIN).json()["accepted"] == first
    story = client.get("/api/v1/accepted").json()["stories"][0]
    assert story["reports"][0]["title"] == "title a" and story["accepted"]["at"] == first["at"]
    assert client.delete("/api/v1/accepted/a", headers=ORIGIN).json() == {"id": "a", "accepted": None}
    assert client.get("/api/v1/accepted").json()["stories"] == []


def test_accepting_an_unknown_story_is_refused(client):
    assert client.post("/api/v1/accepted", json={"id": "nope"}, headers=ORIGIN).status_code == 404
    assert client.post("/api/v1/accepted", json={"id": "bad id!"}, headers=ORIGIN).status_code == 400


@pytest.mark.parametrize("headers", [{}, {"origin": "https://evil.test"}])
def test_writes_from_another_site_are_refused(client, storage, headers):
    storage.save_article(article("a"), None)
    assert client.post("/api/v1/accepted", json={"id": "a"}, headers=headers).status_code == 403
    assert client.delete("/api/v1/accepted/a", headers=headers).status_code == 403
    assert client.post("/api/v1/scrape", headers=headers).status_code == 403


def test_with_reviewing_off_it_is_read_only_and_hides_who(readonly, storage):
    storage.save_article(article("a"), None)
    story = '{"id": "a", "categories": [], "district": "", "score": 1, "reports": []}'
    storage.conn.execute("INSERT INTO accepted VALUES ('a', %s, now(), 'someone')", (story,))
    assert readonly.get("/api/v1/news").status_code == 404
    assert readonly.post("/api/v1/accepted", json={"id": "a"}, headers=ORIGIN).status_code == 403
    assert readonly.post("/api/v1/scrape", headers=ORIGIN).status_code == 403
    body = readonly.get("/api/v1/accepted").json()
    assert body["reviewing"] is False and "by" not in body["stories"][0]["accepted"]


def test_another_host_is_refused(client):
    assert client.get("/api/v1/health", headers={"host": "evil.test"}).status_code == 400


def test_database_down_is_a_plain_503(config, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    app = create_app(config, ScrapeManager(config, idle_run), schedule=False)
    app.state.pool.timeout = 0.2
    with TestClient(app) as c:
        response = c.get("/api/v1/health")
    assert response.status_code == 503 and "error" in response.json()


def test_the_pool_size_comes_from_the_environment(config, database, monkeypatch):
    monkeypatch.setenv("DB_POOL_MAX", "7")
    assert create_app(config, ScrapeManager(config, idle_run), schedule=False).state.pool.max_size == 7


def test_the_scheduler_thread_reads_the_last_run_through_the_pool(client):
    assert client.app.state.scheduler.last_run() is None


def test_browser_calls_are_allowed_from_the_site_only(client):
    site = {"origin": "http://localhost:4321", "access-control-request-method": "POST",
            "access-control-request-headers": "content-type"}
    assert client.options("/api/v1/accepted", headers=site).headers["access-control-allow-origin"] == site["origin"]
    other = {**site, "origin": "https://evil.test"}
    response = client.options("/api/v1/accepted", headers=other)
    assert "access-control-allow-origin" not in response.headers


def test_the_site_can_write_from_its_own_origin(client, storage):
    storage.save_article(article("a"), None)
    made = client.post("/api/v1/accepted", json={"id": "a"}, headers={"origin": "http://localhost:4321"})
    assert made.status_code == 201


def test_scrape_requests_are_rate_limited(client):
    codes = [client.post("/api/v1/scrape", headers=ORIGIN).status_code for _ in range(6)]
    assert codes[-1] == 429 and 429 not in codes[:5]
    assert "Too many" in client.post("/api/v1/scrape", headers=ORIGIN).json()["error"]


def test_health_is_not_rate_limited(client):
    assert {client.get("/api/v1/health").status_code for _ in range(130)} == {200}


# scrapes ---------------------------------------------------------------------
def test_scrape_runs_once_at_a_time_and_reports(config, database, monkeypatch):
    monkeypatch.setenv("AVN_REVIEW", "on")
    release, entered = threading.Event(), threading.Event()

    async def slow(cfg, opts):
        assert opts.since_last_run
        entered.set()
        release.wait(5)
        return RunResult("r", {"outcome": {"saved": 3}})

    with client_for(config, slow) as c:
        assert c.post("/api/v1/scrape", headers=ORIGIN).status_code == 202
        assert entered.wait(5)
        assert c.get("/api/v1/scrape").json()["running"] is True
        assert c.post("/api/v1/scrape", headers=ORIGIN).status_code == 409
        release.set()
        c.app.state.scrapes._thread.join(5)
        state = c.get("/api/v1/scrape").json()
    assert state["ok"] is True and state["running"] is False and state["outcome"] == {"saved": 3}


def test_a_failed_scrape_is_reported(config):
    async def boom(cfg, opts):
        raise RuntimeError("no network")

    scrapes = ScrapeManager(config, boom)
    assert scrapes.start("manual")
    scrapes._thread.join(5)
    assert scrapes.state["ok"] is False and scrapes.state["error"] == "no network"


def test_a_scrape_elsewhere_blocks_the_service(config):
    from avn_scraper.scraper.lock import run_lock
    with run_lock(config.data_dir / ".run.lock"):
        assert ScrapeManager(config, idle_run).start("manual") is False


# schedule --------------------------------------------------------------------
def dhaka(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=DHAKA).astimezone(timezone.utc)


def test_the_latest_slot_is_today_after_7_and_yesterday_before():
    assert latest_slot(dhaka(2, 7), AT_7, DHAKA) == dhaka(2, 7)
    assert latest_slot(dhaka(2, 6, 59), AT_7, DHAKA) == dhaka(1, 7)


@pytest.mark.parametrize("now,last_run,attempt,due", [
    (dhaka(2, 8), dhaka(2, 7, 1), None, False),         # today's run is done
    (dhaka(2, 6), dhaka(1, 7, 1), None, False),         # not 07:00 yet
    (dhaka(2, 7), dhaka(1, 7, 1), None, True),          # 07:00
    (dhaka(2, 15), dhaka(1, 7, 1), None, True),         # the PC came on late
    (dhaka(5, 9), dhaka(1, 7, 1), None, True),          # off for days
    (dhaka(2, 8), None, None, True),                    # never ran
    (dhaka(2, 8), dhaka(1, 7), dhaka(2, 7, 45), False), # just failed: wait
    (dhaka(2, 8), dhaka(1, 7), dhaka(2, 7, 30), True),  # retry after 30 minutes
])
def test_when_a_scrape_is_due(now, last_run, attempt, due):
    assert is_due(now, AT_7, DHAKA, last_run, attempt) is due


class FakeScrapes:
    def __init__(self):
        self.running, self.started = False, []

    def start(self, trigger):
        self.started.append(trigger)
        return True


def test_the_scheduler_waits_for_the_network_and_the_database():
    scrapes = FakeScrapes()
    offline = Scheduler(scrapes, lambda: None, AT_7, DHAKA, online=lambda: False)
    assert offline.tick(dhaka(2, 8)) is False

    def db_down():
        raise OSError("down")

    assert Scheduler(scrapes, db_down, AT_7, DHAKA, online=lambda: True).tick(dhaka(2, 8)) is False
    assert scrapes.started == []

    ready = Scheduler(scrapes, lambda: None, AT_7, DHAKA, online=lambda: True)
    assert ready.tick(dhaka(2, 8)) is True and ready.tick(dhaka(2, 8, 1)) is False
    assert scrapes.started == ["schedule"]


def test_a_running_scrape_is_left_alone():
    scrapes = FakeScrapes()
    scrapes.running = True
    assert Scheduler(scrapes, lambda: None, AT_7, DHAKA, online=lambda: True).tick(dhaka(2, 8)) is False
    assert scrapes.started == []
