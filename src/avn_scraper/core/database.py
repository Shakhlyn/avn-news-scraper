from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


def make_pool(url: str, max_size: int) -> AsyncConnectionPool:
    """Opened by the app's lifespan. Queries wait 5 seconds at most for a connection, and
    Postgres cancels one that runs over 30 seconds (a stuck lock), so a request can't hang."""
    return AsyncConnectionPool(url, min_size=1, max_size=max_size, open=False, timeout=5,
                               kwargs={"autocommit": True, "row_factory": dict_row}, configure=_limit_queries)


async def _limit_queries(conn: psycopg.AsyncConnection) -> None:
    # SET, not the `options` connect argument, which would replace options in DATABASE_URL.
    await conn.execute("SET statement_timeout = 30000")


async def fetch_all(conn: psycopg.AsyncConnection, sql: Any, params: Any = None) -> list[dict[str, Any]]:
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def fetch_one(conn: psycopg.AsyncConnection, sql: Any, params: Any = None) -> dict[str, Any] | None:
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def last_full_run(pool: AsyncConnectionPool) -> datetime | None:
    async with pool.connection() as conn:
        row = await fetch_one(conn, "SELECT max(started_at) AS at FROM runs WHERE finished_at IS NOT NULL "
                                    "AND stats->'only_sources' IS NULL AND stats->'backfill' IS NULL")
    return row["at"]
