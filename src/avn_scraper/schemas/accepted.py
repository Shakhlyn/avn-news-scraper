from __future__ import annotations

from pydantic import BaseModel, Field

from .news import Story

STORY_ID = r"^[A-Za-z0-9_-]{1,64}$"


class Acceptance(BaseModel):
    at: str
    by: str | None = None  # left out with reviewing off


class AcceptedStory(Story):
    accepted: Acceptance


class AcceptedList(BaseModel):
    reviewing: bool
    stories: list[AcceptedStory]


class AcceptRequest(BaseModel):
    id: str = Field(pattern=STORY_ID)


class AcceptResponse(BaseModel):
    id: str
    accepted: Acceptance | None
