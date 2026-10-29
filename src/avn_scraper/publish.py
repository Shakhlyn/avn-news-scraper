"""The news watch file for the AVN site (avnbd.org/dashboard).

`build_news` collects the articles stored in the last `hours` hours (by
`scraped_at`, so it is what the latest runs found), groups reports of one story
by `story_cluster_id`, and trims each report to what the page shows: no bodies,
no images. `upload_r2` puts the JSON in the Cloudflare R2 bucket the site reads,
as `staging/news.json`: the team reviews it on staging.avnbd.org, and avnbd.org
shows only the stories they accept (kept in the site's D1 database).
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from .storage import LocalStorage

NEWS_KEY = "staging/news.json"
SUMMARY_CHARS = 320
R2_ENV_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


def snippet(text: str | None, limit: int = SUMMARY_CHARS) -> str:
    """The first `limit` characters of `text`, cut at a word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:।") + "…"


def safe_url(*candidates: str | None) -> str:
    """The first candidate that is a plain http(s) web address, else "".
    Addresses come from the outlets' pages, so a javascript: or data: link
    must never reach the site's hrefs."""
    for url in candidates:
        url = (url or "").strip()
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme.lower() in ("http", "https") and parts.netloc:
            return url
    return ""


def _report(row: dict[str, Any], cats: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": safe_url(row["canonical_url"], row["url"]),
        "source_id": row["source_id"],
        "source_name": row["source_name"] or row["source_id"],
        "language": row["language"] or "",
        "title": " ".join((row["title"] or "").split()),
        # The outlet's summary, else the start of the body.
        "summary": snippet(row["summary"] or row["body"]),
        "published_at": row["published_at"] or "",
        "district": row["district"] or "",
        "body_missing": bool(row["body_missing"]),
        "categories": cats,
    }


def build_stories(reports: list[dict[str, Any]], clusters: dict[str, str], order: list[str]) -> list[dict[str, Any]]:
    """One story per cluster: its reports newest first and the union of their
    categories in `order`. Stories carried by more outlets come first, then
    the stronger keyword match."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in reports:
        groups[clusters[r["id"]]].append(r)
    rank = {c: i for i, c in enumerate(order)}
    stories = []
    for cluster, group in groups.items():
        group.sort(key=lambda r: (r["published_at"], r["id"]), reverse=True)
        cats = {c["category"] for r in group for c in r["categories"]}
        stories.append({
            "id": cluster,
            "categories": sorted(cats, key=lambda c: (rank.get(c, len(rank)), c)),
            "district": next((r["district"] for r in group if r["district"]), ""),
            "score": max(c["score"] for r in group for c in r["categories"]),
            "reports": group,
        })
    stories.sort(key=lambda s: (-len({r["source_id"] for r in s["reports"]}), -s["score"], s["id"]))
    return stories


def build_news(storage: LocalStorage, category_order: list[str], hours: float = 24,
               now: datetime | None = None) -> dict[str, Any]:
    """The news watch payload for the `hours` hours before `now`. Articles a
    reviewer marked as false positives (review.verified = 0) are left out."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    since = now - timedelta(hours=hours)
    rows = storage.conn.execute(
        "SELECT a.* FROM articles a LEFT JOIN review r ON r.article_id = a.id "
        "WHERE a.scraped_at >= ? AND a.scraped_at <= ? AND (r.verified IS NULL OR r.verified != 0)",
        (since.isoformat(), now.isoformat())).fetchall()
    cats: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ids = [row["id"] for row in rows]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for c in storage.conn.execute(
                f"SELECT article_id, category, score, matched_terms FROM article_categories "
                f"WHERE article_id IN ({','.join('?' * len(chunk))})", chunk):
            cats[c["article_id"]].append({"category": c["category"], "score": c["score"] or 0,
                                          "terms": json.loads(c["matched_terms"] or "[]")})
    reports = [_report(dict(row), sorted(cats[row["id"]], key=lambda c: -c["score"]))
               for row in rows if cats[row["id"]]]
    clusters = {row["id"]: row["story_cluster_id"] or row["id"] for row in rows}
    return {
        "generatedAt": now.isoformat(),
        "windowHours": hours,
        "from": since.isoformat(),
        "to": now.isoformat(),
        "stories": build_stories(reports, clusters, category_order),
    }


def to_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def r2_settings() -> dict[str, str]:
    """R2 credentials and bucket from the environment, never from the repo."""
    missing = [v for v in R2_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"set {', '.join(missing)} to upload to R2")
    return {v: os.environ[v] for v in R2_ENV_VARS}


def upload_r2(body: bytes, settings: dict[str, str] | None = None, client: Any = None) -> str:
    """Put `body` at NEWS_KEY in the bucket and return the key."""
    settings = settings or r2_settings()
    if client is None:
        import boto3  # only the server that uploads needs it

        client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=settings["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=settings["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
    client.put_object(Bucket=settings["R2_BUCKET"], Key=NEWS_KEY, Body=body,
                      ContentType="application/json; charset=utf-8")
    return NEWS_KEY
