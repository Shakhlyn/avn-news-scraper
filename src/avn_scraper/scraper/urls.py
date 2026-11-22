"""URL canonicalization and stable article ids."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "ref", "_ga", "mc_", "igshid", "amp")


def canonicalize(url: str) -> str:
    """Lower-case host, drop fragments, tracking params, 'www.' and trailing slashes."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    if path.endswith("/amp"):
        path = path[:-4] or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(_TRACKING_PREFIXES)]
    return urlunsplit(("https", host, path, urlencode(query), ""))


def article_id(url: str) -> str:
    return hashlib.sha1(canonicalize(url).encode()).hexdigest()[:16]


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def same_site(url: str, homepage: str) -> bool:
    """True when url is on the homepage's site (subdomains of the same base count)."""
    host, base = host_of(url), host_of(homepage)
    return host == base or host.endswith("." + base) or base.endswith("." + host)


# Sections that never carry AVN's topics (killings, torture, disappearances, death penalty,
# border killings, the July uprising, the DSA). Their tabs are not scanned and their links not
# downloaded. Of 1,337 saved articles (2026-11-22), 7 came from such sections, all false matches
# (a gas shop opening, a shooting in Mumbai, explainers on Islamic law). Matched against a whole
# path segment, or its first word ("sports-news", "islamic-life"). "ict" is left out on purpose:
# it is also the International Crimes Tribunal.
OFF_TOPIC_SECTIONS = frozenset({
    "sport", "sports", "cricket", "football", "games", "game", "khela",
    "entertainment", "binodon", "showbiz", "cinema", "movie", "movies", "film", "films", "music", "celebrity",
    "lifestyle", "life-style", "fashion", "food", "recipe", "recipes", "beauty", "travel", "tourism",
    "technology", "tech", "science-tech", "sci-tech", "gadget", "gadgets", "automobile", "auto",
    "business", "economy", "economics", "stock", "stocks", "share-market", "sharebazar", "banking", "finance",
    "corporate", "commerce", "real-estate",
    "jobs", "job", "career", "careers", "religion", "religious", "islam", "islamic", "dharma",
    "horoscope", "rashifal", "astrology", "literature", "sahitya", "arts", "puzzle", "quiz",
    "খেলা", "খেলাধুলা", "বিনোদন", "লাইফস্টাইল", "জীবনযাপন", "জীবনধারা", "ফ্যাশন", "রান্না", "ভ্রমণ",
    "প্রযুক্তি", "তথ্যপ্রযুক্তি", "বিজ্ঞান-প্রযুক্তি", "অর্থনীতি", "বাণিজ্য", "অর্থ-বাণিজ্য", "শেয়ারবাজার",
    "চাকরি", "ধর্ম", "ইসলাম", "রাশিফল", "সাহিত্য",
})


def off_topic_segment(segment: str) -> bool:
    s = segment.lower()
    return s in OFF_TOPIC_SECTIONS or s.split("-")[0] in OFF_TOPIC_SECTIONS


def off_topic_section(url: str) -> bool:
    """True for an article in a section that never carries AVN's topics (sports, entertainment,
    business, ...). Only the section part of the path counts: the last segment of
    `/sports/123` or `/bangladesh/sports-minister-says` is the article itself."""
    segments = [s for s in unquote(urlsplit(url).path).split("/") if s]
    return any(off_topic_segment(s) for s in segments[:-1][:2])


# World-news sections. AVN covers Bangladesh, so their links are downloaded only when the headline
# names Bangladesh, Bangladeshis or a district (geo.about_bangladesh): that keeps Bangladeshis
# executed abroad and the like, and drops the rest. Not here: "country"/"countries" (channel24's
# whole-Bangladesh section) and "probash" (Bangladeshi expatriates).
INTERNATIONAL_SECTIONS = frozenset({
    "international", "world", "world-news", "worldnews", "foreign", "inter-country", "global",
    "asia", "south-asia", "middle-east", "europe", "america", "americas", "africa", "australia",
    "india", "pakistan", "usa", "uk", "antorjatik", "আন্তর্জাতিক", "বিশ্ব", "বিদেশ",
})


def international_section(url: str) -> bool:
    """True for an article in a world-news section (the section part of the path, as above)."""
    segments = [s.lower() for s in unquote(urlsplit(url).path).split("/") if s]
    return any(s in INTERNATIONAL_SECTIONS or s.split("-")[0] in INTERNATIONAL_SECTIONS
               for s in segments[:-1][:2])


def slug_text(url: str) -> str:
    """Readable words from the URL path, used for keyword pre-filtering (Bangla slugs too)."""
    path = unquote(urlsplit(url).path)
    return " ".join(path.replace("-", " ").replace("_", " ").replace("/", " ").split())
