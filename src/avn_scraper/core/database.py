from __future__ import annotations

from datetime import datetime

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(url: str) -> ConnectionPool:
    """Opened by the app's lifespan. Queries wait 5 seconds at most for a connection."""
    return ConnectionPool(url, min_size=1, max_size=4, open=False, timeout=5,
                          kwargs={"autocommit": True, "row_factory": dict_row})


def last_full_run(pool: ConnectionPool) -> datetime | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT max(started_at) AS at FROM runs WHERE finished_at IS NOT NULL "
                           "AND stats->'only_sources' IS NULL").fetchone()
    return row["at"]
