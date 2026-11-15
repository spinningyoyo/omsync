"""Exclusive, non-blocking file locks for operations that must not overlap on this machine.

The lock is held by the operating system on a file in the data directory: flock on macOS and
Linux, msvcrt.locking on Windows. The operating system drops it when the holder exits, even after
a crash, so a stale lock file never blocks anyone. Callers that find the lock held get LockBusy at
once and never wait.
"""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

# Held for the length of `omsync sync` and `omsync receipts fetch`, which both drive Chrome.
SYNC_LOCK_NAME = "sync.lock"
# Held by a running `omsync serve`, so a restore or a second server refuses to start.
SERVE_LOCK_NAME = "serve.lock"


class LockBusy(Exception):
    """Another process holds the lock. Nothing was done."""


if sys.platform == "win32":
    import msvcrt

    def _acquire(handle: IO[str]) -> bool:
        # msvcrt locks a byte range from the current position, so every holder locks byte 0.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _release(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(handle: IO[str]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _release(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on the file at path for the length of the block.

    Raises LockBusy if another process holds it. The file is created if it is missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        if not _acquire(handle):
            raise LockBusy(str(path))
        try:
            yield
        finally:
            _release(handle)
