from __future__ import annotations

from typing import Any

import psycopg

from ..core.database import fetch_all


async def list_runs(conn: psycopg.AsyncConnection, limit: int) -> list[dict[str, Any]]:
    return await fetch_all(
        conn, "SELECT run_id, started_at, finished_at, stats->'outcome' AS outcome, "
              "stats->'only_sources' AS only_sources FROM runs ORDER BY started_at DESC LIMIT %s", (limit,))
