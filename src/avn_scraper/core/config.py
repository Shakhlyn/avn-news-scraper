"""The service's settings, from the environment and .env."""

from __future__ import annotations

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..scraper.config import PROJECT_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    database_url: str
    avn_review: str = ""
    # Sites whose pages may call the API from the browser. No default: each deployment names its own.
    cors_origins: list[str]

    @computed_field
    @property
    def reviewing(self) -> bool:
        """On behaves like staging: all news, Accept, manual scrapes. Off, like production."""
        return self.avn_review == "on"
