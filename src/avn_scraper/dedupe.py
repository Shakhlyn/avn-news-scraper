"""Grouping the same story reported by different outlets."""

from __future__ import annotations

from .normalize import normalize

_STOP = {"the", "a", "an", "of", "in", "on", "to", "for", "and", "at", "by", "with", "is", "as",
         "ও", "এবং", "এর", "থেকে", "করে", "হয়", "না", "যে", "জন্য"}


def title_tokens(title: str) -> frozenset[str]:
    words = (w.strip(".,:;!?'\"()[]‘’“”।-") for w in normalize(title).split())
    return frozenset(w for w in words if len(w) > 1 and w not in _STOP)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_cluster(title: str, recent: list[tuple[str, str, str]], threshold: float = 0.5) -> str | None:
    """recent = [(article_id, cluster_id, title)]; return the cluster of the most similar title."""
    tokens = title_tokens(title)
    best, best_score = None, threshold
    for _id, cluster_id, other in recent:
        score = similarity(tokens, title_tokens(other))
        if score >= best_score:
            best, best_score = cluster_id, score
    return best
