from __future__ import annotations

from pydantic import BaseModel


class CategoryHit(BaseModel):
    category: str
    score: int
    terms: list[str]


class Report(BaseModel):
    id: str
    url: str
    source_id: str
    source_name: str
    language: str
    title: str
    summary: str
    published_at: str
    district: str
    body_missing: bool
    categories: list[CategoryHit]


class Story(BaseModel):
    id: str
    categories: list[str]
    district: str
    score: int
    reports: list[Report]


class SourceCount(BaseModel):
    id: str
    name: str | None
    count: int


class TopicCount(BaseModel):
    key: str
    count: int


class NewsPage(BaseModel):
    generatedAt: str
    latest: bool
    total: int
    page: int
    pages: int
    stories: list[Story]
    sources: list[SourceCount]
    topics: list[TopicCount]
