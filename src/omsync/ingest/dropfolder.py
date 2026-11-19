"""The drop folder: where statement files are kept, and which of them are not imported yet.

Layout under <data_dir>/statements/:

    <INSTITUTION>/                          OFX files. The account comes from the file's ACCTID.
    <INSTITUTION>/<STATEMENT_ACCOUNT_ID>/   formats with no account number, such as BoA CSV.

The institution key is the folder name lowercased, so BOA is the key boa. The statement account
ID is the subfolder name, usually the last four digits of the account. Hidden files and folders,
whose names start with a dot, are ignored. A file in any other place under statements/ has no
institution, so importing it needs --institution.

Connectors and people put files in the same places. Nothing here writes until stage_external,
which copies a file from outside the data directory into the folder.
"""

import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.ingest.formats.base import sha256_of
from omsync.ledger.statements import StatementFile

_INSTITUTION_KEY = re.compile(r"[a-z0-9][a-z0-9_-]*")
_ACCOUNT_FOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass(frozen=True)
class PendingFile:
    """A statement file in the drop folder. institution_key is None when the file is not in an
    institution folder. statement_account_id is None unless the file is in an account subfolder."""

    institution_key: str | None
    statement_account_id: str | None
    path: Path


def pending_files(
    session: Session, settings: Settings, institution_key: str | None = None
) -> list[PendingFile]:
    """Every visible file under statements/ whose SHA-256 is not yet a statement file, or only
    those in one institution's folder. Read-only."""
    root = settings.statements_dir.resolve()
    if not root.is_dir():
        return []
    imported = set(session.scalars(select(StatementFile.sha256)).all())
    pending: list[PendingFile] = []
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if not path.is_file() or any(part.startswith(".") for part in parts):
            continue
        location = _location(parts, path)
        if institution_key not in (None, location.institution_key):
            continue
        if sha256_of(path.read_bytes()) in imported:
            continue
        pending.append(location)
    return pending


def location_of(settings: Settings, path: Path) -> PendingFile | None:
    """Where a file sits in the drop folder, or None if it is outside statements/."""
    try:
        parts = path.resolve().relative_to(settings.statements_dir.resolve()).parts
    except ValueError:
        return None
    return _location(parts, path.resolve())


def destination_for(
    settings: Settings,
    source: Path,
    institution_key: str,
    statement_account_id: str | None = None,
    year: int | str | None = None,
) -> Path:
    """Where stage_external would copy this file. Writes nothing.

    A name already taken in the folder gets a -N suffix, starting at -1. Raises ValueError if
    the institution key or statement account ID could not name a folder safely.
    """
    folder = _institution_folder(settings, institution_key)
    account_slug = statement_account_id or "default"
    if not _ACCOUNT_FOLDER.fullmatch(account_slug):
        raise ValueError(f"statement account ID {account_slug!r} is not a folder name")
    folder = folder / account_slug
    if year is None:
        year = date.today().year
    folder = folder / str(year)
    return _free_name(folder, source.name)


def stage_external(
    path: Path,
    settings: Settings,
    institution_key: str,
    statement_account_id: str | None = None,
    year: int | str | None = None,
) -> Path:
    """Copy a file from outside the data directory into the drop folder, and return the copy.

    The copy never overwrites: a name already taken gets a -N suffix. Used only on --apply.
    """
    destination = destination_for(settings, path, institution_key, statement_account_id, year=year)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target)
    return destination


def stored_path(settings: Settings, path: Path) -> str:
    """The path a statement file is recorded under: relative to the data directory if inside it."""
    try:
        return path.resolve().relative_to(settings.data_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _location(parts: tuple[str, ...], path: Path) -> PendingFile:
    if len(parts) >= 3:
        return PendingFile(parts[0].lower(), parts[1], path)
    if len(parts) == 2:
        return PendingFile(parts[0].lower(), None, path)
    return PendingFile(None, None, path)


def _institution_folder(settings: Settings, institution_key: str) -> Path:
    """The existing folder for this key, whatever its case, or a new one named in capitals."""
    if not _INSTITUTION_KEY.fullmatch(institution_key):
        raise ValueError(
            f"institution key {institution_key!r} must be lowercase letters, digits, - or _"
        )
    root = settings.statements_dir.resolve()
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and child.name.lower() == institution_key:
                return child
    return root / institution_key.upper()


def _free_name(folder: Path, name: str) -> Path:
    candidate = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while candidate.exists():
        candidate = folder / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate
