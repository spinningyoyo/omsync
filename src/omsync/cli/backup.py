"""`omsync backup`: create, list, verify, and restore financial archives."""

from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import typer

from omsync.cli.common import fail, load_settings_or_exit
from omsync.cli.groups import backup_app
from omsync.cli.system import upgrade_database
from omsync.core.locks import SERVE_LOCK_NAME, SYNC_LOCK_NAME, LockBusy, exclusive_lock
from omsync.core.recovery import (
    ArchiveInfo,
    RecoveryError,
    RestorePlan,
    check_archive,
    create_archive,
    list_archives,
    restore_archive,
)
from omsync.core.schema import head_revision
from omsync.core.settings import Settings, resolve_config_path

_SYNC_BUSY = "a sync or receipt download is running. Try again when it finishes."
_SERVE_RUNNING = "omsync serve is running. Stop it, then restore."


@backup_app.command("create")
def backup_create() -> None:
    """Write an archive of the database, statements, receipts, and omsync.toml into backups/."""
    settings = load_settings_or_exit()
    try:
        with exclusive_lock(settings.data_dir / SYNC_LOCK_NAME):
            info = create_archive(settings)
    except LockBusy:
        fail(_SYNC_BUSY)
    except RecoveryError as exc:
        fail(str(exc))
    typer.echo(
        f"Backed up {info.file_count} files to {info.path} "
        f"(database revision {info.database_revision})."
    )
    typer.echo("The archive holds your statements, receipts, and finances. Keep it private.")


@backup_app.command("list")
def backup_list() -> None:
    """List the archives in backups/, newest first."""
    settings = load_settings_or_exit()
    archives = list_archives(settings)
    if not archives:
        typer.echo("No archives yet. Make one with `omsync backup create`.")
        return
    typer.echo(
        f"{'created (UTC)':<20}  {'kind':<11}  {'revision':<14}  {'files':>5}  {'size':>9}  name"
    )
    for info in archives:
        typer.echo(_row(info))


@backup_app.command("restore")
def backup_restore(
    archive: Annotated[
        str, typer.Argument(help="An archive path, or the file name of one in backups/.")
    ],
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Replace the current database, statements, and receipts. Without it, only check.",
        ),
    ] = False,
) -> None:
    """Restore an archive. Without --yes it checks the archive and changes nothing.

    With --yes: the archive is verified first, then the current state is saved as a pre-restore
    archive, then the database, statements and receipts are replaced. A failed archive check
    changes nothing. Exits 1 if the archive is damaged, is from a newer omsync, or a sync or
    `omsync serve` is running.
    """
    settings = load_settings_or_exit()
    path = _archive_path(settings, archive)
    try:
        with ExitStack() as held:
            _hold(held, settings, SERVE_LOCK_NAME, _SERVE_RUNNING)
            _hold(held, settings, SYNC_LOCK_NAME, _SYNC_BUSY)
            if not yes:
                _echo_plan(check_archive(settings, path))
                typer.echo("Nothing was changed. Run again with --yes to restore.")
                return
            result = restore_archive(settings, path, config_path=resolve_config_path())
            if result.safety_archive is not None:
                typer.echo(f"Saved the state from before the restore to {result.safety_archive}.")
            _echo_restore(result.plan, path)
            _echo_config(result.config_status, result.config_path)
            if result.plan.revision != head_revision():
                typer.echo(
                    f"The archived database is at revision {result.plan.revision}. Upgrading."
                )
                upgrade_database(settings)
    except RecoveryError as exc:
        fail(str(exc))
    typer.echo("Restore complete.")


def _hold(stack: ExitStack, settings: Settings, name: str, message: str) -> None:
    try:
        stack.enter_context(exclusive_lock(settings.data_dir / name))
    except LockBusy:
        fail(message)


def _archive_path(settings: Settings, archive: str) -> Path:
    candidate = Path(archive).expanduser()
    if not candidate.is_file():
        candidate = settings.backups_dir / archive
    if not candidate.is_file():
        fail(f"there is no archive at {archive}. Run `omsync backup list` to see them.")
    return candidate


def _echo_plan(plan: RestorePlan) -> None:
    typer.echo(
        f"The archive checks out: database at revision {plan.revision} "
        f"(this omsync's head is {plan.head}), {_files(plan.statement_files, 'statement')}, "
        f"{_files(plan.receipt_files, 'receipt')}."
    )
    typer.echo(
        "A restore would replace the current database, statements and receipts. "
        "The current state is saved first."
    )


def _echo_restore(plan: RestorePlan, archive: Path) -> None:
    typer.echo(
        f"Restored the database (revision {plan.revision}), "
        f"{_files(plan.statement_files, 'statement')} and "
        f"{_files(plan.receipt_files, 'receipt')} from {archive.name}."
    )


def _echo_config(status: str, path: Path | None) -> None:
    if status == "written":
        typer.echo(f"Wrote omsync.toml from the archive to {path}.")
    elif status == "kept":
        typer.echo(f"Kept the existing config file at {path}; the archive's copy was not used.")
    elif status == "failed":
        typer.echo(f"Could not write omsync.toml to {path}. Copy it there by hand.", err=True)


def _row(info: ArchiveInfo) -> str:
    return (
        f"{info.created_at.replace('T', ' ').rstrip('Z'):<20}  {info.label:<11}  "
        f"{info.database_revision:<14}  {info.file_count:>5}  {_size(info.size):>9}  "
        f"{info.path.name}"
    )


def _files(count: int, kind: str = "") -> str:
    noun = " ".join(part for part in (kind, "file" if count == 1 else "files") if part)
    return f"{count} {noun}"


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024**2:
        return f"{count / 1024:.1f} KB"
    return f"{count / 1024**2:.1f} MB"
