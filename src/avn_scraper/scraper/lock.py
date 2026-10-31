"""One run at a time: the service and a manual `avn run` must not overlap."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RunInProgress(Exception):
    pass


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Exclusive, non-blocking lock; released automatically if the process dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            raise RunInProgress(fh.read().strip() or "another run") from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()}")
        fh.flush()
        yield
    finally:
        fh.close()
