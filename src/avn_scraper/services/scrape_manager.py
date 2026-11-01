"""Scrapes inside the service: one at a time, as a task on the API's event loop. Their
blocking work runs in threads, so the API keeps answering. The file lock also keeps out
`avn run` and ./scrape-now.sh."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from ..scraper.config import Config
from ..scraper.lock import RunInProgress, run_lock
from ..scraper.pipeline import RunOptions, RunResult, run_pipeline

log = logging.getLogger(__name__)

Runner = Callable[[Config, RunOptions], Awaitable[RunResult]]


class ScrapeManager:
    def __init__(self, cfg: Config, runner: Runner = run_pipeline):
        self.cfg = cfg
        self.runner = runner
        self.lock_path = cfg.data_dir / ".run.lock"
        self._task: asyncio.Task[None] | None = None
        self.state: dict[str, Any] = {"running": False, "trigger": None, "started_at": None,
                                      "finished_at": None, "ok": None, "error": None, "outcome": None}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, trigger: str, opts: RunOptions | None = None) -> bool:
        """Start a scrape. False if one is already going, here or in another process.
        Call it on the event loop: nothing here awaits, so two callers cannot both start."""
        if self.running:
            return False
        try:
            with run_lock(self.lock_path):
                pass
        except RunInProgress:
            return False
        self.state = {"running": True, "trigger": trigger,
                      "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "finished_at": None, "ok": None, "error": None, "outcome": None}
        self._task = asyncio.create_task(self._work(opts or RunOptions(since_last_run=True)), name="scrape")
        return True

    async def stop(self) -> None:
        """Stop a scrape still going when the service shuts down."""
        if self.running:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _work(self, opts: RunOptions) -> None:
        result: dict[str, Any] = {"ok": False}
        try:
            with run_lock(self.lock_path):
                run = await self.runner(self.cfg, opts)
            result = {"ok": True, "outcome": run.stats.get("outcome")}
        except Exception as exc:
            log.exception("scrape failed")
            result["error"] = str(exc) or type(exc).__name__
        self.state.update(result, running=False,
                          finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
