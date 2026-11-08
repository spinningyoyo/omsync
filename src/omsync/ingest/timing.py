"""How long each step of a sync took, kept beside the data so a slow sync can be explained."""

import contextlib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from omsync.core.settings import load_settings

TIMING_FILE = "sync-timing.log"


@contextmanager
def timed(source: str, step: str) -> Iterator[None]:
    """Append one line with the step's duration, whether or not it succeeded. A line holds only a
    connector or provider key, a step name, and seconds."""
    started = time.monotonic()
    try:
        yield
    finally:
        seconds = time.monotonic() - started
        path = load_settings().data_dir / TIMING_FILE
        with contextlib.suppress(OSError), path.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {source} {step} {seconds:.1f}s\n")
