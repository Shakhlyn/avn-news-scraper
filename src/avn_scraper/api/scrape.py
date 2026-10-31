"""Scrapes inside the service: one at a time, in their own thread so the API keeps
answering. The file lock also keeps out `avn run` and ./scrape-now.sh."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from ..config import Config
from ..lock import RunInProgress, run_lock
from ..pipeline import RunOptions, RunResult, run_pipeline

log = logging.getLogger(__name__)

Runner = Callable[[Config, RunOptions], Awaitable[RunResult]]


class ScrapeManager:
    def __init__(self, cfg: Config, runner: Runner = run_pipeline):
        self.cfg = cfg
        self.runner = runner
        self.lock_path = cfg.data_dir / ".run.lock"
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"running": False, "trigger": None, "started_at": None,
                                      "finished_at": None, "ok": None, "error": None, "outcome": None}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, trigger: str, opts: RunOptions | None = None) -> bool:
        """Start a scrape. False if one is already going, here or in another process."""
        with self._guard:
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
            self._thread = threading.Thread(target=self._work, args=(opts or RunOptions(since_last_run=True),),
                                            name="scrape", daemon=True)
            self._thread.start()
            return True

    def _work(self, opts: RunOptions) -> None:
        result: dict[str, Any] = {"ok": False}
        try:
            with run_lock(self.lock_path):
                run = asyncio.run(self.runner(self.cfg, opts))
            result = {"ok": True, "outcome": run.stats.get("outcome")}
        except Exception as exc:
            log.exception("scrape failed")
            result["error"] = str(exc) or type(exc).__name__
        self.state.update(result, running=False,
                          finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
