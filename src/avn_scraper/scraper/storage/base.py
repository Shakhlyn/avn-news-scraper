"""Storage interface, implemented by PostgresStorage."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from typing import Any

import pandas as pd

from ..classify import Match
from ..config import Source


class Storage(ABC):
    @abstractmethod
    async def upsert_sources(self, sources: Iterable[Source]) -> None: ...

    @abstractmethod
    async def known_ids(self, ids: Iterable[str], *, include_old: bool = True) -> set[str]:
        """Ids already stored as articles or already checked and rejected.
        include_old=False: links skipped only for being older than a run's window don't count."""

    @abstractmethod
    async def mark_seen(self, rows: Iterable[tuple[str, str, str]]) -> None:
        """Record (id, url, status) for URLs checked but not stored."""

    @abstractmethod
    async def recent_titles(self, days: int) -> list[tuple[str, str, str]]:
        """[(article_id, story_cluster_id, title)] for recent articles."""

    @abstractmethod
    async def titles_published(self, start: datetime, end: datetime) -> list[tuple[str, str, str]]:
        """[(article_id, story_cluster_id, title)] for articles published in [start, end)."""

    @abstractmethod
    async def backfill_progress(self, periods: Iterable[Any] | None = None,
                                exact: bool = False) -> list[dict[str, Any]]:
        """Rows of backfill_progress: all, those overlapping the given periods (objects with
        .start and .end dates), or with exact=True those of exactly these periods."""

    @abstractmethod
    async def save_backfill_progress(self, row: dict[str, Any]) -> None:
        """Insert or replace one (source_id, month, until) row."""

    @abstractmethod
    async def save_article(self, record: dict[str, Any], raw_html: str | None) -> None: ...

    @abstractmethod
    async def replace_categories(self, article_id: str, matches: list[Match]) -> None: ...

    @abstractmethod
    def iter_articles(self) -> AsyncIterator[dict[str, Any]]: ...

    @abstractmethod
    async def save_run(self, run_id: str, started_at: str, finished_at: str, stats: dict[str, Any]) -> None: ...

    @abstractmethod
    async def last_full_run(self) -> datetime | None:
        """When the last finished run over all outlets started."""

    @abstractmethod
    async def known_block_logs(self) -> set[str]:
        """Run ids whose block log is already imported."""

    @abstractmethod
    async def save_block_log(self, parsed: dict[str, Any]) -> None:
        """Store one parsed block-log file (blocklog.parse_file) in one transaction."""

    @abstractmethod
    async def query_df(self, sql: str, params: tuple = ()) -> pd.DataFrame: ...
