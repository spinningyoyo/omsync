"""Exclusive, non-blocking file locks for operations that must not overlap on this machine.

The lock is an flock on a file in the data directory. The kernel drops it when the holder exits,
even after a crash, so a stale lock file never blocks anyone. Callers that find the lock held get
LockBusy at once and never wait.
"""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Held for the length of `omsync sync` and `omsync receipts fetch`, which both drive Chrome.
SYNC_LOCK_NAME = "sync.lock"
# Held by a running `omsync serve`, so a restore or a second server refuses to start.
SERVE_LOCK_NAME = "serve.lock"


class LockBusy(Exception):
    """Another process holds the lock. Nothing was done."""


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on the file at path for the length of the block.

    Raises LockBusy if another process holds it. The file is created if it is missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy(str(path)) from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
