"""Site-specific extractors for outlets whose article pages are rendered by JavaScript.

For these sites the HTML holds only a headline and a shortened summary; the text
comes from a JSON API the page calls. An adapter maps the article URL to that API
URL and parses the response into the same `Extracted` record as `extract.extract`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .extract import Extracted

_DHAKA = ZoneInfo("Asia/Dhaka")


@dataclass(frozen=True)
class Adapter:
    name: str
    api_url: Callable[[str], str | None]
    parse: Callable[[bytes], Extracted]


def _html_text(html: str | None) -> str:
    if not html:
        return ""
    tree = HTMLParser(html)
    paragraphs = [" ".join(p.text().split()) for p in tree.css("p")]
    text = "\n".join(p for p in paragraphs if p)
    return text or " ".join(tree.text(separator=" ").split())


# -- Channel 24 -------------------------------------------------------------
# https://www.channel24bd.tv/<category>/article/<id>/<slug>
#   -> https://backoffice.channel24bd.tv/api/v2/content-details/<category>/<id>
_C24_URL = re.compile(r"^https?://(?:www\.)?channel24bd\.tv/([A-Za-z0-9_-]+)/article/(\d+)")


def _channel24_api(url: str) -> str | None:
    m = _C24_URL.match(url)
    return f"https://backoffice.channel24bd.tv/api/v2/content-details/{m[1]}/{m[2]}" if m else None


def _channel24_parse(content: bytes) -> Extracted:
    data = json.loads(content)
    item = data["contentDetails"][0]
    created = item.get("create_date")
    writer = (data.get("writerInfo") or {}).get("WriterName") or item.get("WriterName") or ""
    return Extracted(
        title=(item.get("ContentHeading") or "").strip(),
        body=_html_text(item.get("ContentDetails")),
        summary=(item.get("ContentBrief") or "").strip(),
        author=writer.strip(),
        # create_date is Dhaka wall-clock time without an offset
        published_at=datetime.fromisoformat(created).replace(tzinfo=_DHAKA) if created else None,
        canonical_url=None,
        image=None,
    )


ADAPTERS = [Adapter("channel24", _channel24_api, _channel24_parse)]


def find_adapter(url: str) -> tuple[Adapter, str] | None:
    """(adapter, api_url) for URLs handled by a site adapter, else None."""
    for adapter in ADAPTERS:
        api = adapter.api_url(url)
        if api:
            return adapter, api
    return None
