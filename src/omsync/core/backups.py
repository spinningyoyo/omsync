"""Database backups: copies of omsync.db, made with SQLite's backup API and named by kind and time.

Automatic backups come from `db upgrade` and `serve`, before a migration runs. Only the newest
KEEP_AUTOMATIC are kept. Manual backups, from `db backup`, are never pruned. Files in backups/
that do not match the naming scheme are never touched.

The SQLite backup API is used rather than a file copy, because the database runs in WAL mode and
a plain copy could miss committed pages still in the -wal file.
"""

import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

BackupKind = Literal["auto", "manual"]
AUTOMATIC: Final[BackupKind] = "auto"
MANUAL: Final[BackupKind] = "manual"
KEEP_AUTOMATIC: Final = 10

# omsync-<kind>-<UTC timestamp>[-<counter>].db. The counter separates two backups in one second.
_NAME = re.compile(r"omsync-(auto|manual)-(\d{8}T\d{6}Z)(?:-(\d+))?\.db")


def back_up(db_path: Path, backups_dir: Path, kind: BackupKind) -> Path | None:
    """Copy the database to backups/omsync-<kind>-<UTC timestamp>.db, or return None if absent.

    On failure, the partial file is removed and the error is raised.
    """
    if not db_path.is_file():
        return None
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = backups_dir / f"omsync-{kind}-{stamp}.db"
    counter = 1
    while target.exists():
        target = backups_dir / f"omsync-{kind}-{stamp}-{counter}.db"
        counter += 1
    try:
        snapshot_database(db_path, target)
    except sqlite3.Error:
        target.unlink(missing_ok=True)
        raise
    return target


def snapshot_database(db_path: Path, target: Path) -> None:
    """Write a consistent copy of the database to target, with SQLite's backup API.

    target must not exist. Raises sqlite3.Error if the copy fails; the caller removes the file.
    """
    with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(target)) as dest:
        source.backup(dest)


def prune_automatic(backups_dir: Path, keep: int = KEEP_AUTOMATIC) -> list[Path]:
    """Delete all but the newest `keep` automatic backups, and return the paths deleted.

    Newest means the latest timestamp, with the counter breaking ties within one second.
    Manual backups are not counted and not deleted.
    """
    if not backups_dir.is_dir():
        return []
    automatic: list[tuple[str, int, Path]] = []
    for path in backups_dir.iterdir():
        match = _NAME.fullmatch(path.name)
        if match is None or match.group(1) != AUTOMATIC or not path.is_file():
            continue
        automatic.append((match.group(2), int(match.group(3) or 0), path))
    automatic.sort(reverse=True)
    removed: list[Path] = []
    for _stamp, _counter, path in automatic[keep:]:
        path.unlink()
        removed.append(path)
    return removed
