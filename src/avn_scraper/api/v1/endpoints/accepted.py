"""The review desk. A story is copied from the database when accepted, never from
the request, so a reviewer can choose stories but not write them. With reviewing off
the API is read-only and leaves out who accepted what."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from ....core.limiter import WRITE_LIMIT, limiter
from ....core.security import require_reviewing, same_origin
from ....repositories import accepted as repo
from ....repositories.news import find_story
from ....schemas.accepted import STORY_ID, AcceptedList, AcceptRequest, AcceptResponse
from ...deps import CategoryOrder, CurrentSettings, Db

router = APIRouter(prefix="/accepted", tags=["accepted"])
guarded = [Depends(require_reviewing), Depends(same_origin)]


def _when(row: dict[str, Any], reviewing: bool) -> dict[str, str]:
    stamp = row["accepted_at"].isoformat()
    return {"at": stamp, "by": row["accepted_by"]} if reviewing else {"at": stamp}


@router.get("", response_model=AcceptedList, response_model_exclude_none=True)
def list_accepted(conn: Db, settings: CurrentSettings):
    reviewing = settings.reviewing
    return {"reviewing": reviewing,
            "stories": [{**r["story"], "accepted": _when(r, reviewing)} for r in repo.list_accepted(conn)]}


@router.post("", status_code=201, response_model=AcceptResponse, dependencies=guarded)
@limiter.limit(WRITE_LIMIT)
def accept(request: Request, body: AcceptRequest, conn: Db, order: CategoryOrder):
    story = find_story(conn, body.id, order)
    if story is None:
        raise HTTPException(404, "That story is not in the news database.")
    return {"id": body.id, "accepted": _when(repo.add_accepted(conn, body.id, story, ""), True)}


@router.delete("/{story_id}", response_model=AcceptResponse, dependencies=guarded)
@limiter.limit(WRITE_LIMIT)
def withdraw(request: Request, story_id: Annotated[str, Path(pattern=STORY_ID)], conn: Db):
    repo.remove_accepted(conn, story_id)
    return {"id": story_id, "accepted": None}
