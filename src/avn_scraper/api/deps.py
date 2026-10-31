"""What the routes share."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import psycopg
from fastapi import Depends, Request

from ..core.config import Settings
from ..services.scrape_manager import ScrapeManager


def get_db(request: Request) -> Iterator[psycopg.Connection]:
    with request.app.state.pool.connection() as conn:
        yield conn


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_scrapes(request: Request) -> ScrapeManager:
    return request.app.state.scrapes


def get_category_order(request: Request) -> list[str]:
    return request.app.state.category_order


Db = Annotated[psycopg.Connection, Depends(get_db)]
CurrentSettings = Annotated[Settings, Depends(get_settings)]
Scrapes = Annotated[ScrapeManager, Depends(get_scrapes)]
CategoryOrder = Annotated[list[str], Depends(get_category_order)]
