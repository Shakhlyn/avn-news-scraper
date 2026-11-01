from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg

from ..core.database import fetch_all, fetch_one
from ..services.stories import build_stories, to_report

LISTED = ("COALESCE(r.verified, 1) <> 0 AND EXISTS "
          "(SELECT 1 FROM article_categories c WHERE c.article_id = a.id)")
STORY = "COALESCE(NULLIF(a.story_cluster_id, ''), a.id)"
HITS = "FROM articles a LEFT JOIN review r ON r.article_id = a.id"


@dataclass
class NewsFilters:
    start: date | None = None
    end: date | None = None
    source: str = ""
    topic: str = ""
    q: str = ""
    page: int = 1
    limit: int = 50

    @property
    def latest(self) -> bool:
        return not (self.start or self.end or self.q)


def like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def where_clause(f: NewsFilters, now: datetime, skip: str = "") -> tuple[str, dict[str, Any]]:
    """With no dates and no search, the last 24 hours of scraping; dates or a search
    reach all of the history. `skip` leaves one filter out, for that filter's choices."""
    conds, params = [LISTED], {}
    if f.start:
        conds.append("a.published_date >= %(start)s")
        params["start"] = f.start
    if f.end:
        conds.append("a.published_date <= %(end)s")
        params["end"] = f.end
    if f.latest:
        conds.append("a.scraped_at >= %(since)s")
        params["since"] = now - timedelta(hours=24)
    if f.source and skip != "source":
        conds.append("a.source_id = %(source)s")
        params["source"] = f.source
    if f.topic and skip != "topic":
        conds.append("EXISTS (SELECT 1 FROM article_categories c "
                     "WHERE c.article_id = a.id AND c.category = %(topic)s)")
        params["topic"] = f.topic
    if f.q:
        conds.append("(a.title ILIKE %(q)s OR a.summary ILIKE %(q)s OR a.body ILIKE %(q)s)")
        params["q"] = like_pattern(f.q)
    return " AND ".join(conds), params


async def _reports(conn: psycopg.AsyncConnection, where: str, params: dict[str, Any]):
    rows = await fetch_all(
        conn,
        f"""SELECT a.id, a.url, a.canonical_url, a.source_id, a.source_name, a.language, a.title,
                   a.summary, a.body, a.published_at, a.district, a.body_missing, {STORY} AS story
            {HITS} WHERE {where}""", params)
    cats: dict[str, list[dict[str, Any]]] = {}
    if rows:
        for c in await fetch_all(
                conn, "SELECT article_id, category, score, matched_terms FROM article_categories "
                      "WHERE article_id = ANY(%s)", ([r["id"] for r in rows],)):
            cats.setdefault(c["article_id"], []).append(
                {"category": c["category"], "score": c["score"] or 0, "terms": c["matched_terms"] or []})
    clusters = {r["id"]: r["story"] for r in rows}
    return [to_report(r, cats.get(r["id"], [])) for r in rows], lambda report: clusters[report["id"]]


async def list_news(conn: psycopg.AsyncConnection, f: NewsFilters, order: list[str],
                    now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    where, params = where_clause(f, now)
    page = await fetch_all(
        conn,
        f"""WITH hits AS (SELECT a.id, a.source_id, {STORY} AS story {HITS} WHERE {where})
            SELECT h.story
            FROM hits h JOIN article_categories c ON c.article_id = h.id
            GROUP BY h.story
            ORDER BY count(DISTINCT h.source_id) DESC, max(c.score) DESC, h.story
            LIMIT %(limit)s OFFSET %(offset)s""",
        {**params, "limit": f.limit, "offset": (f.page - 1) * f.limit})
    ids = [r["story"] for r in page]
    total = (await fetch_one(conn, f"SELECT count(DISTINCT {STORY}) AS n {HITS} WHERE {where}", params))["n"]

    stories: list[dict[str, Any]] = []
    if ids:
        reports, cluster_of = await _reports(conn, f"{where} AND {STORY} = ANY(%(ids)s)", {**params, "ids": ids})
        built = {s["id"]: s for s in build_stories(reports, cluster_of, order)}
        stories = [built[i] for i in ids]

    by_source, source_params = where_clause(f, now, "source")
    sources = await fetch_all(
        conn,
        f"SELECT a.source_id AS id, max(a.source_name) AS name, count(*) AS count "
        f"{HITS} WHERE {by_source} GROUP BY a.source_id ORDER BY name", source_params)
    by_topic, topic_params = where_clause(f, now, "topic")
    topics = await fetch_all(
        conn,
        f"SELECT c.category AS key, count(DISTINCT {STORY}) AS count "
        f"{HITS} JOIN article_categories c ON c.article_id = a.id "
        f"WHERE {by_topic} GROUP BY c.category", topic_params)

    return {
        "generatedAt": now.isoformat(),
        "latest": f.latest,
        "total": total,
        "page": f.page,
        "pages": max(1, -(-total // f.limit)),
        "stories": stories,
        "sources": sources,
        "topics": topics,
    }


async def find_story(conn: psycopg.AsyncConnection, story_id: str, order: list[str]) -> dict[str, Any] | None:
    """Every report of a story, whichever day it came in."""
    reports, cluster_of = await _reports(conn, f"{LISTED} AND {STORY} = %(id)s", {"id": story_id})
    return build_stories(reports, cluster_of, order)[0] if reports else None
