"""The checks on writes."""

from __future__ import annotations

from fastapi import HTTPException, Request


async def require_reviewing(request: Request) -> None:
    if not request.app.state.settings.reviewing:
        raise HTTPException(403, "Stories are accepted on the review desk only.")


async def same_origin(request: Request) -> None:
    """Writes come from the desk's own pages only: a browser always sends Origin on a
    POST or DELETE, and another site cannot fake it."""
    host = request.headers.get("host", "")
    allowed = {f"http://{host}", f"https://{host}", *request.app.state.settings.cors_origins}
    if request.headers.get("origin") not in allowed:
        raise HTTPException(403, "Cross-site request refused.")
