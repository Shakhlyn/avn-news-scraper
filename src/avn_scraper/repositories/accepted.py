from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def list_accepted(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM accepted ORDER BY accepted_at DESC, id").fetchall()


def add_accepted(conn: psycopg.Connection, story_id: str, story: dict[str, Any], by: str) -> dict[str, Any]:
    """A story accepted twice keeps its first acceptance."""
    conn.execute(
        "INSERT INTO accepted (id, story, accepted_at, accepted_by) VALUES (%s, %s, now(), %s) "
        "ON CONFLICT (id) DO NOTHING", (story_id, Jsonb(story), by))
    return conn.execute("SELECT * FROM accepted WHERE id = %s", (story_id,)).fetchone()


def remove_accepted(conn: psycopg.Connection, story_id: str) -> None:
    conn.execute("DELETE FROM accepted WHERE id = %s", (story_id,))
