"""What the routes share: the database, the reviewing switch, the write check."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
from fastapi import Depends, HTTPException, Request


def get_db(request: Request) -> Iterator[psycopg.Connection]:
    with request.app.state.pool.connection() as conn:
        yield conn


Db = Depends(get_db)


def require_reviewing(request: Request) -> None:
    if not request.app.state.reviewing:
        raise HTTPException(403, "Stories are accepted on the review desk only.")


def same_origin(request: Request) -> None:
    """Writes come from the desk's own pages only: a browser always sends Origin on a
    POST or DELETE, and another site cannot fake it."""
    host = request.headers.get("host", "")
    if request.headers.get("origin") not in (f"http://{host}", f"https://{host}"):
        raise HTTPException(403, "Cross-site request refused.")
