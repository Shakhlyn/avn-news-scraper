"""The daily scrape, inside the service. It checks once a minute instead of sleeping
until 07:00, so a PC that was off, asleep or offline at 07:00 catches up by itself:
any time the latest slot has no full run after it, a scrape is due."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .scrape_manager import ScrapeManager

log = logging.getLogger(__name__)

TICK_SECONDS = 60
RETRY_AFTER = timedelta(minutes=30)
PROBE_HOST = "www.prothomalo.com"


def latest_slot(now: datetime, daily_at: time, tz: ZoneInfo) -> datetime:
    """The most recent `daily_at` in `tz` at or before `now`."""
    local = now.astimezone(tz)
    slot = datetime.combine(local.date(), daily_at, tz)
    if slot > local:
        slot = datetime.combine(local.date() - timedelta(days=1), daily_at, tz)
    return slot.astimezone(timezone.utc)


def is_due(now: datetime, daily_at: time, tz: ZoneInfo, last_run: datetime | None,
           last_attempt: datetime | None) -> bool:
    slot = latest_slot(now, daily_at, tz)
    if last_run is not None and last_run >= slot:
        return False
    return last_attempt is None or now - last_attempt >= RETRY_AFTER


def network_up() -> bool:
    try:
        socket.getaddrinfo(PROBE_HOST, 443)
        return True
    except OSError:
        return False


class Scheduler:
    def __init__(self, scrapes: ScrapeManager, last_run: Callable[[], datetime | None],
                 daily_at: time, tz: ZoneInfo, online: Callable[[], bool] = network_up):
        self.scrapes = scrapes
        self.last_run = last_run
        self.daily_at = daily_at
        self.tz = tz
        self.online = online
        self.last_attempt: datetime | None = None

    def tick(self, now: datetime | None = None) -> bool:
        """Start the scrape if one is due. True if it started."""
        now = now or datetime.now(timezone.utc)
        if self.scrapes.running:
            return False
        try:
            last = self.last_run()
        except Exception as exc:  # the database is still starting: try again next tick
            log.warning("schedule check failed: %s", exc)
            return False
        if not is_due(now, self.daily_at, self.tz, last, self.last_attempt) or not self.online():
            return False
        if not self.scrapes.start("schedule"):
            return False
        self.last_attempt = now
        log.info("scheduled scrape started")
        return True

    async def loop(self) -> None:
        while True:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(TICK_SECONDS)
