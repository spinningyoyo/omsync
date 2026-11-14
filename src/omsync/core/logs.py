"""The log file in the data directory, for the traceback of an unexpected error."""

import logging
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "omsync.log"
MAX_BYTES = 1_000_000
BACKUPS = 3


class _TypesOnlyFormatter(logging.Formatter):
    """Writes the frames and exception types of an error, never its message. The message of an
    unexpected error can hold a form value, a URL with a token, or a line of a statement."""

    def formatException(
        self,
        ei: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        _, exc, tb = ei
        parts: list[str] = []
        seen: set[int] = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            kind = type(exc)
            parts.append(
                "Traceback (most recent call last):\n"
                + "".join(traceback.format_tb(exc.__traceback__ if exc is not ei[1] else tb))
                + f"{kind.__module__}.{kind.__qualname__}\n"
            )
            exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        return "".join(reversed(parts)).rstrip("\n")


def log_path(data_dir: Path) -> Path:
    return data_dir / LOG_DIR_NAME / LOG_FILE_NAME


def setup_logging(data_dir: Path) -> None:
    """Send omsync's log records to a size-capped file in data_dir. A second call moves the file."""
    root = logging.getLogger("omsync")
    for handler in list(root.handlers):
        if getattr(handler, "_omsync_file", False):
            root.removeHandler(handler)
            handler.close()
    path = log_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, delay=True)
    except OSError:
        return  # a log that cannot be written must not stop the app
    handler._omsync_file = True  # type: ignore[attr-defined]
    handler.setFormatter(_TypesOnlyFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
