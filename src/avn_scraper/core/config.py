"""The service's settings, from the environment and .env."""

from __future__ import annotations

from pydantic import PositiveInt, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..scraper.config import PROJECT_ROOT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    database_url: str
    avn_review: str = ""
    # Sites whose pages may call the API from the browser. No default: each deployment names its own.
    cors_origins: list[str]
    # The names this service answers to, as they arrive in the Host header. No default: each
    # deployment names its own. A page elsewhere can point a domain of its own at this
    # machine (DNS rebinding); its requests then carry that domain as Host.
    allowed_hosts: list[str]
    # The most database connections the API holds at once.
    db_pool_max: PositiveInt

    @field_validator("allowed_hosts")
    @classmethod
    def no_wildcard(cls, hosts: list[str]) -> list[str]:
        """same_origin trusts the Host header, so "*" would let any site write."""
        if "*" in hosts:
            raise ValueError("name the hosts; * is not allowed")
        return hosts

    @computed_field
    @property
    def reviewing(self) -> bool:
        """On behaves like staging: all news, Accept, manual scrapes. Off, like production."""
        return self.avn_review == "on"
