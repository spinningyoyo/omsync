"""doctor reports missing setup. serve runs the web app. The db commands manage the file."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from alembic import command

from omsync.cli.common import fail, load_settings_or_exit
from omsync.cli.groups import app, db_app
from omsync.core import backups
from omsync.core.db import database_url
from omsync.core.doctor import run_checks
from omsync.core.locks import SERVE_LOCK_NAME, LockBusy, exclusive_lock
from omsync.core.schema import alembic_config, head_revision
from omsync.core.settings import Settings, load_settings
from omsync.web.app import create_app


@app.command()
def doctor() -> None:
    """Report anything missing for the app to run. Exits 1 if anything is missing."""
    try:
        settings = load_settings()
    except ValueError as exc:
        typer.echo(f"MISSING config file: {exc}")
        raise typer.Exit(code=1) from exc

    checks = run_checks(settings)
    for check in checks:
        typer.echo(check.render())
    if not all(check.ok for check in checks):
        raise typer.Exit(code=1)


@app.command()
def serve() -> None:
    """Upgrade the database as `db upgrade` does, then run the web app on the configured host.

    Holds serve.lock while it runs, so `omsync backup restore` refuses to replace the data under a
    running server. A second server exits 1 instead of starting.
    """
    settings = load_settings_or_exit()
    try:
        with exclusive_lock(settings.data_dir / SERVE_LOCK_NAME):
            upgrade_database(settings)
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    except LockBusy:
        fail("omsync serve is already running, or a restore is in progress. Stop it first.")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Create the data directory, back up omsync.db if it exists, then migrate to the head."""
    settings = load_settings_or_exit()
    backup = upgrade_database(settings)
    if backup is not None:
        typer.echo(f"Backed up {settings.db_path} to {backup}")
    typer.echo(f"Database {settings.db_path} is at revision {head_revision()}")


@db_app.command("backup")
def db_backup() -> None:
    """Copy omsync.db to backups/omsync-manual-<UTC time>.db. Manual backups are never pruned."""
    settings = load_settings_or_exit()
    backup = backups.back_up(settings.db_path, settings.backups_dir, backups.MANUAL)
    if backup is None:
        fail(f"there is no database at {settings.db_path} to back up")
    typer.echo(f"Backed up {settings.db_path} to {backup}")


@db_app.command("snapshot")
def db_snapshot(
    target: Annotated[
        Path,
        typer.Argument(help="A new or empty directory, which becomes the copy's data directory."),
    ],
) -> None:
    """Copy omsync.db into another data directory and migrate only the copy to the head.

    The configured database is read, never written or migrated, so the copy is safe to inspect or
    change. Point any command at it with OMSYNC_DATA_DIR.
    """
    settings = load_settings_or_exit()
    if not settings.db_path.is_file():
        fail(f"there is no database at {settings.db_path} to copy")
    target = target.expanduser().resolve()
    if target == settings.data_dir.resolve():
        fail("the snapshot directory must not be the configured data directory")
    if target.exists() and any(target.iterdir()):
        fail(f"{target} is not empty")
    target.mkdir(parents=True, exist_ok=True)
    copy = target / settings.db_path.name
    try:
        backups.snapshot_database(settings.db_path, copy)
    except sqlite3.Error as exc:
        copy.unlink(missing_ok=True)
        fail(f"could not copy {settings.db_path}: {exc}")
    copied_at = _revision(copy)
    command.upgrade(alembic_config(f"sqlite:///{copy}"), "head")
    typer.echo(f"Copied {settings.db_path} at revision {copied_at} to {copy}")
    typer.echo(f"The copy is at revision {head_revision()}. Use it with OMSYNC_DATA_DIR={target}")


def _revision(db_path: Path) -> str:
    with closing(sqlite3.connect(db_path)) as connection:
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return "none"
    return row[0] if row else "none"


def upgrade_database(settings: Settings) -> Path | None:
    """Migrate the settings' database to the latest revision and return the backup path, if any.

    A missing data directory is created. An existing database is copied first, as an automatic
    backup, and automatic backups beyond the newest KEEP_AUTOMATIC are deleted.
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    backup = backups.back_up(settings.db_path, settings.backups_dir, backups.AUTOMATIC)
    if backup is not None:
        pruned = backups.prune_automatic(settings.backups_dir)
        if pruned:
            typer.echo(
                f"Deleted {len(pruned)} old automatic backup(s), "
                f"keeping the newest {backups.KEEP_AUTOMATIC}"
            )
    command.upgrade(alembic_config(database_url(settings)), "head")
    return backup
