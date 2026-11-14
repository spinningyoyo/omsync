"""Financial archive creation, verification, and recoverable file replacement."""

import hashlib
import io
import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
import tomllib
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from omsync import __version__
from omsync.core.backups import snapshot_database
from omsync.core.schema import head_revision, known_revisions
from omsync.core.settings import Settings

ARCHIVE_FORMAT = 1
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "omsync.db"
DATA_PREFIX = "data/"  # archive members that belong in the data directory
CONFIG_NAME = "config/omsync.toml"  # the config file, when one was in use
RESTORED_DIRS = ("statements", "receipts")
MANUAL = "manual"
PRE_RESTORE = "pre-restore"
ARCHIVE_NAME = re.compile(r"omsync-backup-(\d{8}T\d{6}Z)(?:-(\d+))?(-pre-restore)?\.tar\.gz")
CHUNK = 1 << 20

# What a restore replaces. The SQLite side files are moved aside with the database and never
# restored: a snapshot is complete on its own.
REPLACED = (DATABASE_NAME, f"{DATABASE_NAME}-wal", f"{DATABASE_NAME}-shm", *RESTORED_DIRS)
RESTORED = (DATABASE_NAME, *RESTORED_DIRS)


class RecoveryError(Exception):
    """An archive cannot be made or restored. The message names the problem and holds no
    statement contents, account numbers, or secrets. A restore that raises changed nothing."""


@dataclass(frozen=True)
class ArchiveInfo:
    path: Path
    label: str
    created_at: str
    omsync_version: str
    database_revision: str
    file_count: int
    size: int


@dataclass(frozen=True)
class RestorePlan:
    """What a restore would bring back, after the archive has been checked."""

    revision: str
    head: str
    statement_files: int
    receipt_files: int
    has_config: bool


@dataclass(frozen=True)
class RestoreResult:
    plan: RestorePlan
    safety_archive: Path | None
    config_status: str  # "written", "kept", "failed", or "none"
    config_path: Path | None


@dataclass(frozen=True)
class _Extracted:
    manifest: dict[str, Any]
    root: Path  # holds the restored items: root/omsync.db, root/statements, root/receipts
    config: Path | None
    revision: str
    statement_files: int
    receipt_files: int


def create_archive(settings: Settings, *, label: str = MANUAL) -> ArchiveInfo:
    """Write a new archive of the data directory into backups/, and return what it holds.

    Needs a database. The caller holds the sync lock, so the files are read at one point in time.
    On failure no archive is left behind.
    """
    if not settings.db_path.is_file():
        raise RecoveryError(f"there is no database at {settings.db_path} to back up")
    backups = settings.backups_dir
    backups.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=backups) as scratch:
        snapshot = Path(scratch) / DATABASE_NAME
        try:
            snapshot_database(settings.db_path, snapshot)
        except sqlite3.Error as exc:
            raise RecoveryError(
                f"the database could not be copied ({type(exc).__name__})"
            ) from None
        revision = database_revision(snapshot) or "none"
        entries = [(DATA_PREFIX + DATABASE_NAME, snapshot), *_data_entries(settings)]
        if settings.config_source is not None and settings.config_source.is_file():
            entries.append((CONFIG_NAME, settings.config_source))
        files = [
            {"name": name, "sha256": _sha256(path), "size": path.stat().st_size}
            for name, path in entries
        ]
        manifest = {
            "format": ARCHIVE_FORMAT,
            "label": label,
            "created_at": _utc_stamp(),
            "omsync_version": __version__,
            "database_revision": revision,
            "files": files,
        }
        target = _new_archive_path(backups, label)
        partial = target.with_name(f".{target.name}.partial")
        try:
            with tarfile.open(partial, "w:gz") as tar:
                body = json.dumps(manifest, indent=2).encode("utf-8")
                member = tarfile.TarInfo(MANIFEST_NAME)
                member.size = len(body)
                member.mtime = int(time.time())
                tar.addfile(member, io.BytesIO(body))
                for name, path in entries:
                    tar.add(path, arcname=name, recursive=False)
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    return _info(target, manifest)


def list_archives(settings: Settings) -> list[ArchiveInfo]:
    """Every archive in backups/, newest first. An archive that cannot be read is still listed."""
    if not settings.backups_dir.is_dir():
        return []
    found: list[ArchiveInfo] = []
    for path in settings.backups_dir.iterdir():
        if ARCHIVE_NAME.fullmatch(path.name) is None or not path.is_file():
            continue
        try:
            found.append(_info(path, read_manifest(path)))
        except RecoveryError:
            found.append(_info(path, None))
    return sorted(found, key=lambda info: (info.created_at, info.path.name), reverse=True)


def read_manifest(archive: Path) -> dict[str, Any]:
    """The archive's manifest, without extracting anything."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            handle = tar.extractfile(tar.getmember(MANIFEST_NAME))
            if handle is None:
                raise KeyError(MANIFEST_NAME)
            raw = handle.read()
    except (tarfile.TarError, KeyError, OSError):
        raise RecoveryError(f"{archive.name} is not a readable omsync archive") from None
    return _parse_manifest(raw)


def check_archive(settings: Settings, archive: Path) -> RestorePlan:
    """Verify an archive and say what restoring it would bring back. Changes nothing that
    outlives the call: the archive is unpacked into a scratch folder, and the folder is removed."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".restore-", dir=settings.data_dir))
    try:
        extracted = _extract_verified(archive, scratch)
        return _plan(extracted)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def restore_archive(
    settings: Settings, archive: Path, *, config_path: Path | None
) -> RestoreResult:
    """Replace the database, statements and receipts with the archive's, after a safety archive.

    The caller holds the sync and serve locks. Everything the archive holds is verified before the
    first file moves. If the swap fails, the moves already made are undone, and the error says so.
    omsync.toml is written to config_path only when no file is there yet; an existing one is kept.
    """
    data = settings.data_dir
    data.mkdir(parents=True, exist_ok=True)
    if not settings.db_path.is_file() and any((data / name).exists() for name in RESTORED_DIRS):
        raise RecoveryError(
            "statements or receipts exist without a database, so there is no safety archive for "
            "them. Move them out of the data directory by hand, then restore."
        )
    scratch = Path(tempfile.mkdtemp(prefix=".restore-", dir=data))
    try:
        extracted = _extract_verified(archive, scratch)
        plan = _plan(extracted)
        has_database = settings.db_path.is_file()
        safety = create_archive(settings, label=PRE_RESTORE).path if has_database else None
        _swap(data, scratch, extracted)
        config_status = _write_config(extracted, config_path)
    except BaseException:
        if not _previous_is_unrecovered(scratch):
            shutil.rmtree(scratch, ignore_errors=True)
        raise
    shutil.rmtree(scratch, ignore_errors=True)
    return RestoreResult(
        plan=plan,
        safety_archive=safety,
        config_status=config_status,
        config_path=config_path,
    )


def database_revision(path: Path) -> str | None:
    """The Alembic revision recorded in a SQLite file, or None if it has none."""
    try:
        with closing(sqlite3.connect(path)) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _plan(extracted: _Extracted) -> RestorePlan:
    head = head_revision()
    if extracted.revision not in known_revisions():
        raise RecoveryError(
            f"the archive's database is at revision {extracted.revision}, which this omsync does "
            f"not know (its head is {head}). The archive was made by a newer omsync; upgrade "
            "omsync before restoring it."
        )
    return RestorePlan(
        revision=extracted.revision,
        head=head,
        statement_files=extracted.statement_files,
        receipt_files=extracted.receipt_files,
        has_config=extracted.config is not None,
    )


def _extract_verified(archive: Path, scratch: Path) -> _Extracted:
    """Unpack every member into scratch, checking each against the manifest and the name rules.

    Raises RecoveryError on the first problem. Nothing outside scratch is written.
    """
    root = scratch / "new"
    root.mkdir()
    try:
        tar = tarfile.open(archive, "r:gz")  # noqa: SIM115 - the `with tar:` below closes it
    except (tarfile.TarError, OSError):
        raise RecoveryError(f"{archive.name} is not a readable omsync archive") from None
    with tar:
        by_name = _index_members(tar)
        manifest = _verified_manifest(tar, by_name)
        entries = manifest["files"]
        listed = [entry["name"] for entry in entries]
        if len(set(listed)) != len(listed) or set(listed) != set(by_name) - {MANIFEST_NAME}:
            raise RecoveryError("the manifest does not list exactly the files in the archive")
        if DATA_PREFIX + DATABASE_NAME not in listed:
            raise RecoveryError("the archive has no database; nothing restored")
        for index, entry in enumerate(entries, start=1):
            destination = _destination(root, scratch, entry["name"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            _extract_member(tar, by_name[entry["name"]], entry, destination, index)
    revision = _verified_database_revision(root / DATABASE_NAME)
    config = scratch / "config.toml" if CONFIG_NAME in listed else None
    if config is not None:
        _check_config(config)
    return _Extracted(
        manifest=manifest,
        root=root,
        config=config,
        revision=revision,
        statement_files=sum(name.startswith(f"{DATA_PREFIX}statements/") for name in listed),
        receipt_files=sum(name.startswith(f"{DATA_PREFIX}receipts/") for name in listed),
    )


def _index_members(tar: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    try:
        members = tar.getmembers()
    except tarfile.TarError:
        raise RecoveryError("the archive is damaged and cannot be read") from None
    by_name: dict[str, tarfile.TarInfo] = {}
    for index, member in enumerate(members, start=1):
        if not member.isreg():
            raise RecoveryError(f"archive entry #{index} is not a plain file; nothing restored")
        if member.name in by_name:
            raise RecoveryError(f"archive entry #{index} appears twice; nothing restored")
        if member.name != MANIFEST_NAME:
            _check_member_name(member.name, index)
        by_name[member.name] = member
    return by_name


def _verified_manifest(tar: tarfile.TarFile, by_name: dict[str, tarfile.TarInfo]) -> dict[str, Any]:
    if MANIFEST_NAME not in by_name:
        raise RecoveryError("the archive has no manifest; nothing restored")
    return _parse_manifest(_read_member(tar, by_name[MANIFEST_NAME]))


def _check_member_name(name: str, index: int) -> None:
    """Only the database, the two trees and the config may be restored. Absolute paths, `..`,
    backslashes, and any other name are refused."""
    path = PurePosixPath(name)
    if name == CONFIG_NAME:
        return
    canonical = "/".join(path.parts) == name
    if not name.startswith(DATA_PREFIX) or not canonical or "\\" in name or ".." in path.parts:
        raise RecoveryError(f"archive entry #{index} is not an allowed path; nothing restored")
    relative = PurePosixPath(name[len(DATA_PREFIX) :]).parts
    if relative == (DATABASE_NAME,) or (len(relative) >= 2 and relative[0] in RESTORED_DIRS):
        return
    raise RecoveryError(f"archive entry #{index} is not an allowed path; nothing restored")


def _destination(root: Path, scratch: Path, name: str) -> Path:
    if name == CONFIG_NAME:
        return scratch / "config.toml"
    return root / name[len(DATA_PREFIX) :]


def _extract_member(
    tar: tarfile.TarFile, member: tarfile.TarInfo, entry: dict[str, Any], dest: Path, index: int
) -> None:
    source = tar.extractfile(member)
    if source is None:
        raise RecoveryError(f"archive entry #{index} cannot be read; nothing restored")
    digest = hashlib.sha256()
    size = 0
    with dest.open("wb") as out:
        while chunk := source.read(CHUNK):
            digest.update(chunk)
            out.write(chunk)
            size += len(chunk)
    if digest.hexdigest() != entry["sha256"] or size != entry["size"]:
        raise RecoveryError(
            f"file #{index} in the archive does not match its checksum. The archive is damaged "
            "or was edited. Nothing was restored."
        )


def _read_member(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    source = tar.extractfile(member)
    if source is None:
        raise RecoveryError("the archive's manifest cannot be read; nothing restored")
    return source.read()


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(raw)
    except ValueError:
        raise RecoveryError("the archive's manifest is damaged") from None
    if not isinstance(manifest, dict) or manifest.get("format") != ARCHIVE_FORMAT:
        raise RecoveryError("this archive's format is not one this omsync reads")
    files = manifest.get("files")
    well_formed = isinstance(files, list) and all(
        isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("sha256"), str)
        and isinstance(entry.get("size"), int)
        for entry in files
    )
    if not well_formed:
        raise RecoveryError("the archive's manifest is damaged")
    return manifest


def _verified_database_revision(path: Path) -> str:
    try:
        with closing(sqlite3.connect(path)) as connection:
            check = connection.execute("PRAGMA integrity_check").fetchone()[0]
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error:
        raise RecoveryError("the archive's database cannot be read; nothing restored") from None
    if check != "ok" or row is None:
        raise RecoveryError("the archive's database is not an omsync database; nothing restored")
    return str(row[0])


def _check_config(path: Path) -> None:
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise RecoveryError(
            "the archive's omsync.toml is not valid TOML; nothing restored"
        ) from None


def _swap(data: Path, scratch: Path, extracted: _Extracted) -> None:
    """Move the current items aside into scratch/previous, then the archived ones in.

    If any move fails, the moves already made are reversed. scratch/previous is left in place only
    if the reversal itself fails, so nothing is lost.
    """
    previous = scratch / "previous"
    previous.mkdir()
    moved_aside: list[str] = []
    moved_in: list[str] = []
    try:
        for name in REPLACED:
            source = data / name
            if source.exists() or source.is_symlink():
                source.rename(previous / name)
                moved_aside.append(name)
        for name in RESTORED:
            staged = extracted.root / name
            if staged.exists():
                staged.rename(data / name)
                moved_in.append(name)
    except BaseException:  # noqa: BLE001 - undo the moves on any failure, then re-raise
        try:
            for name in reversed(moved_in):
                (data / name).rename(extracted.root / name)
            for name in moved_aside:
                (previous / name).rename(data / name)
        except OSError as exc:
            raise RecoveryError(
                f"the restore failed and could not be undone ({type(exc).__name__}). The previous "
                f"data is in {previous}. Move it back by hand."
            ) from None
        raise RecoveryError(
            "the restore failed part way; the previous data is back in place"
        ) from None


def _previous_is_unrecovered(scratch: Path) -> bool:
    previous = scratch / "previous"
    return previous.is_dir() and any(previous.iterdir())


def _write_config(extracted: _Extracted, config_path: Path | None) -> str:
    if extracted.config is None or config_path is None:
        return "none"
    if config_path.exists():
        return "kept"
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = config_path.with_name(f"{config_path.name}.restore")
        shutil.copyfile(extracted.config, temporary)
        temporary.replace(config_path)
    except OSError:
        return "failed"
    return "written"


def _data_entries(settings: Settings) -> Iterator[tuple[str, Path]]:
    """Every regular file under statements/ and receipts/, as archive names. A symbolic link is
    refused, because it could point outside the data directory."""
    for dirname in RESTORED_DIRS:
        root = settings.data_dir / dirname
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise RecoveryError(f"{dirname}/ is not a plain folder; the archive was not made")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise RecoveryError(
                    f"a link in {dirname}/ cannot be archived; the archive was not made"
                )
            if path.is_file():
                yield f"{DATA_PREFIX}{path.relative_to(settings.data_dir).as_posix()}", path


def _info(path: Path, manifest: dict[str, Any] | None) -> ArchiveInfo:
    match = ARCHIVE_NAME.fullmatch(path.name)
    stamp = match.group(1) if match else ""
    created = (
        datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else ""
    )
    label = PRE_RESTORE if match and match.group(3) else MANUAL
    if manifest is None:
        return ArchiveInfo(path, label, created, "?", "unreadable", 0, path.stat().st_size)
    return ArchiveInfo(
        path=path,
        label=str(manifest.get("label", label)),
        created_at=str(manifest.get("created_at", created)),
        omsync_version=str(manifest.get("omsync_version", "?")),
        database_revision=str(manifest.get("database_revision", "?")),
        file_count=len(manifest["files"]),
        size=path.stat().st_size,
    )


def _new_archive_path(backups: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{PRE_RESTORE}" if label == PRE_RESTORE else ""
    counter = 0
    while True:
        middle = f"-{counter}" if counter else ""
        target = backups / f"omsync-backup-{stamp}{middle}{suffix}.tar.gz"
        if not target.exists():
            return target
        counter += 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
