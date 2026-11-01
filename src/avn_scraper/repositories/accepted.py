from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..core.database import fetch_all, fetch_one


async def list_accepted(conn: psycopg.AsyncConnection) -> list[dict[str, Any]]:
    return await fetch_all(conn, "SELECT * FROM accepted ORDER BY accepted_at DESC, id")


async def add_accepted(conn: psycopg.AsyncConnection, story_id: str, story: dict[str, Any], by: str) -> dict[str, Any]:
    """A story accepted twice keeps its first acceptance."""
    await conn.execute(
        "INSERT INTO accepted (id, story, accepted_at, accepted_by) VALUES (%s, %s, now(), %s) "
        "ON CONFLICT (id) DO NOTHING", (story_id, Jsonb(story), by))
    return await fetch_one(conn, "SELECT * FROM accepted WHERE id = %s", (story_id,))


async def remove_accepted(conn: psycopg.AsyncConnection, story_id: str) -> None:
    await conn.execute("DELETE FROM accepted WHERE id = %s", (story_id,))
