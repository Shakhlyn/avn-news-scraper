"""The AVN news service: the news API, the review desk's API, and the scraper's
control and daily schedule. Run it with `avn serve`."""

from .app import create_app

__all__ = ["create_app"]
