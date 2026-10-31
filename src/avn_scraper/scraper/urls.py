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


def slug_text(url: str) -> str:
    """Readable words from the URL path, used for keyword pre-filtering (Bangla slugs too)."""
    path = unquote(urlsplit(url).path)
    return " ".join(path.replace("-", " ").replace("_", " ").replace("/", " ").split())
