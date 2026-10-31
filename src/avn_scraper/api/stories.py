"""Articles to stories: one story per story_cluster_id, with reports trimmed to what
the page shows (no bodies, no images). Plain functions, tested without a database."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit

SUMMARY_CHARS = 320


def snippet(text: str | None, limit: int = SUMMARY_CHARS) -> str:
    """The first `limit` characters of `text`, cut at a word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:।") + "…"


def safe_url(*candidates: str | None) -> str:
    """The first candidate that is a plain http(s) address, else "". Addresses come
    from the outlets' pages, so a javascript: or data: link must never reach an href."""
    for url in candidates:
        url = (url or "").strip()
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme.lower() in ("http", "https") and parts.netloc:
            return url
    return ""


def to_report(row: dict[str, Any], categories: list[dict[str, Any]]) -> dict[str, Any]:
    published = row["published_at"]
    return {
        "id": row["id"],
        "url": safe_url(row["canonical_url"], row["url"]),
        "source_id": row["source_id"],
        "source_name": row["source_name"] or row["source_id"],
        "language": row["language"] or "",
        "title": " ".join((row["title"] or "").split()),
        # The outlet's summary, else the start of the body.
        "summary": snippet(row["summary"] or row["body"]),
        "published_at": published.isoformat() if isinstance(published, datetime) else published or "",
        "district": row["district"] or "",
        "body_missing": bool(row["body_missing"]),
        "categories": sorted(categories, key=lambda c: -c["score"]),
    }


def build_stories(reports: list[dict[str, Any]], cluster_of: Callable[[dict[str, Any]], str],
                  order: list[str]) -> list[dict[str, Any]]:
    """One story per cluster: reports newest first and the union of their categories
    in `order`. Stories carried by more outlets come first, then the stronger match."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in reports:
        groups[cluster_of(r)].append(r)
    rank = {c: i for i, c in enumerate(order)}
    stories = []
    for cluster, group in groups.items():
        group.sort(key=lambda r: (r["published_at"], r["id"]), reverse=True)
        cats = {c["category"] for r in group for c in r["categories"]}
        stories.append({
            "id": cluster,
            "categories": sorted(cats, key=lambda c: (rank.get(c, len(rank)), c)),
            "district": next((r["district"] for r in group if r["district"]), ""),
            "score": max((c["score"] for r in group for c in r["categories"]), default=0),
            "reports": group,
        })
    stories.sort(key=lambda s: (-len({r["source_id"] for r in s["reports"]}), -s["score"], s["id"]))
    return stories
