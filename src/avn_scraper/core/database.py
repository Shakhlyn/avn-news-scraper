from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def make_pool(url: str, max_size: int) -> AsyncConnectionPool:
    """Opened by the app's lifespan. Queries wait 5 seconds at most for a connection."""
    return AsyncConnectionPool(url, min_size=1, max_size=max_size, open=False, timeout=5,
                               kwargs={"autocommit": True, "row_factory": dict_row})


async def fetch_all(conn: psycopg.AsyncConnection, sql: Any, params: Any = None) -> list[dict[str, Any]]:
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def fetch_one(conn: psycopg.AsyncConnection, sql: Any, params: Any = None) -> dict[str, Any] | None:
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def last_full_run(pool: AsyncConnectionPool) -> datetime | None:
    async with pool.connection() as conn:
        row = await fetch_one(conn, "SELECT max(started_at) AS at FROM runs WHERE finished_at IS NOT NULL "
                                    "AND stats->'only_sources' IS NULL")
    return row["at"]
