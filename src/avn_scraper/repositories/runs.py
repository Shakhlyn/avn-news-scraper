from __future__ import annotations

from typing import Any

import psycopg


def list_runs(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT run_id, started_at, finished_at, stats->'outcome' AS outcome, "
        "stats->'only_sources' AS only_sources FROM runs ORDER BY started_at DESC LIMIT %s", (limit,)).fetchall()
