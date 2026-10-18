"""Keyword-rule classification of articles into topic categories."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .normalize import BANGLA_RANGE, has_bangla, normalize, normalize_unicode

# A term may not start in the middle of a word (Latin or Bangla).
_LEFT = rf"(?<![\w{BANGLA_RANGE}])"
# English terms must end at a word boundary (an "s" plural is allowed). Bangla terms
# take suffixes (গুলি -> গুলিতে), so they have no right boundary.
_RIGHT_EN = r"s?(?![a-z0-9])"


@dataclass(frozen=True)
class Term:
    text: str
    pattern: re.Pattern[str]
    weight: int
    strong: bool = False


@dataclass
class Category:
    key: str
    label: str
    terms: list[Term]
    exclude: list[Term] = field(default_factory=list)


@dataclass
class Match:
    category: str
    score: int
    matched_terms: list[str]


def compile_term(raw: str, weight: int = 1, strong: bool = False) -> Term:
    """Compile a keyword ("re:" prefix = raw regex) into a pattern for normalized text."""
    raw = str(raw)
    if raw.startswith("re:"):
        pattern = re.compile(normalize_unicode(raw[3:]), re.IGNORECASE)
        return Term(raw, pattern, weight, strong)
    text = normalize(raw)
    body = r"\s+".join(re.escape(part) for part in text.split(" "))
    right = "" if has_bangla(text) else _RIGHT_EN
    return Term(raw, re.compile(_LEFT + body + right), weight, strong)


def _count(pattern: re.Pattern[str], text: str, cap: int) -> int:
    n = 0
    for _ in pattern.finditer(text):
        n += 1
        if n >= cap:
            break
    return n


class Classifier:
    def __init__(self, keywords: dict[str, Any]):
        s = keywords.get("settings", {})
        self.min_score = int(s.get("min_score", 4))
        self.title_bonus = int(s.get("title_bonus", 2))
        self.body_cap = int(s.get("body_cap", 3))
        self.require_strong = bool(s.get("require_strong", True))
        strong_w = int(s.get("strong_weight", 4))
        weak_w = int(s.get("weak_weight", 1))

        self.categories: list[Category] = []
        for key, spec in (keywords.get("categories") or {}).items():
            terms = [compile_term(t, strong_w, strong=True) for t in spec.get("strong") or []]
            terms += [compile_term(t, weak_w) for t in spec.get("weak") or []]
            exclude = [compile_term(t) for t in spec.get("exclude") or []]
            self.categories.append(Category(key, spec.get("label", key), terms, exclude))

        self.labels = {c.key: c.label for c in self.categories}

    def classify(self, title: str = "", body: str = "", summary: str = "") -> list[Match]:
        """Score full article text; return matching categories, best first."""
        t = normalize(title)
        b = normalize(f"{summary}\n{body}")
        matches: list[Match] = []
        for cat in self.categories:
            if any(x.pattern.search(t) or x.pattern.search(b) for x in cat.exclude):
                continue
            score, hits, strong_hit = 0, [], False
            for term in cat.terms:
                in_title = bool(term.pattern.search(t))
                n_body = _count(term.pattern, b, self.body_cap)
                if not (in_title or n_body):
                    continue
                score += term.weight * n_body + (term.weight * self.title_bonus if in_title else 0)
                hits.append(term.text)
                strong_hit = strong_hit or term.strong
            if score >= self.min_score and (strong_hit or not self.require_strong):
                matches.append(Match(cat.key, score, hits))
        return sorted(matches, key=lambda m: -m.score)

    def prefilter(self, *texts: str | None) -> bool:
        """Cheap check on headline/summary/URL slug: does any term of any category occur?"""
        text = normalize(" ".join(t for t in texts if t))
        if not text:
            return False
        return any(term.pattern.search(text) for cat in self.categories for term in cat.terms)
