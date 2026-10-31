"""The review desk. A story is copied from the database when accepted, never from
the request, so a reviewer can choose stories but not write them. With reviewing off
the API is read-only and leaves out who accepted what."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from .. import repository as repo
from ..deps import Db, require_reviewing, same_origin

STORY_ID = r"^[A-Za-z0-9_-]{1,64}$"

router = APIRouter(prefix="/api/accepted", tags=["accepted"])
guarded = [Depends(require_reviewing), Depends(same_origin)]


class Accept(BaseModel):
    id: str = Field(pattern=STORY_ID)


def _when(row: dict[str, Any], reviewing: bool) -> dict[str, str]:
    at: datetime = row["accepted_at"]
    stamp = at.isoformat()
    return {"at": stamp, "by": row["accepted_by"]} if reviewing else {"at": stamp}


@router.get("")
def accepted(request: Request, conn: psycopg.Connection = Db) -> dict[str, Any]:
    reviewing = request.app.state.reviewing
    return {"reviewing": reviewing,
            "stories": [{**r["story"], "accepted": _when(r, reviewing)} for r in repo.list_accepted(conn)]}


@router.post("", status_code=201, dependencies=guarded)
def accept(body: Accept, request: Request, conn: psycopg.Connection = Db) -> dict[str, Any]:
    story = repo.find_story(conn, body.id, request.app.state.category_order)
    if story is None:
        raise HTTPException(404, "That story is not in the news database.")
    return {"id": body.id, "accepted": _when(repo.add_accepted(conn, body.id, story, ""), True)}


@router.delete("/{story_id}", dependencies=guarded)
def withdraw(story_id: Annotated[str, Path(pattern=STORY_ID)], conn: psycopg.Connection = Db) -> dict[str, Any]:
    repo.remove_accepted(conn, story_id)
    return {"id": story_id, "accepted": None}
