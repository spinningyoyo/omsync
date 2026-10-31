"""doctor reports missing setup. serve runs the web app. The db commands manage the file."""

import dataclasses
import logging
import shutil
import signal
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
import uvicorn
from alembic import command
from sqlalchemy.orm import Session

from omsync.cli.common import fail, load_settings_or_exit
from omsync.cli.groups import app, db_app
from omsync.core import backups
from omsync.core.db import database_url, make_engine
from omsync.core.doctor import run_checks
from omsync.core.locks import SERVE_LOCK_NAME, LockBusy, exclusive_lock
from omsync.core.schema import alembic_config, head_revision
from omsync.core.settings import Settings, load_settings
from omsync.demo import is_demo_dir
from omsync.demo.build import build_demo
from omsync.demo.site import DemoSite
from omsync.demo.vault import DemoVault
from omsync.reconcile.auto import reconcile_after_import
from omsync.web.app import create_app

log = logging.getLogger(__name__)


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
def serve(
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Serve the made-up demo, even when a ledger exists."),
    ] = False,
) -> None:
    """Run the web app on the configured host.

    With a ledger, upgrade it as `db upgrade` does, run the automatic step once, then serve it.
    The automatic step is the one that follows an import (see omsync.reconcile.auto). It runs
    here so that what a migration or an edit made outside the app left undone is caught up before
    the first page. A failure is logged and never stops the server.

    Holds serve.lock while it runs, so `omsync backup restore` refuses to replace the data under a
    running server. A second server exits 1 instead of starting.

    Without a ledger, or with --demo, serve the demo: a made-up person's year of money, built in a
    temporary directory that is deleted when the server stops. The configured data directory is
    never created, opened, migrated, or locked in demo mode.
    """
    settings = load_settings_or_exit()
    if demo or not settings.db_path.is_file():
        if not demo:
            typer.echo(
                f"No ledger was found at {settings.db_path}, so this is the demo, with made-up "
                "data. To start a real ledger, see Settings → Guides → Start your own ledger."
            )
        _serve_demo(settings)
        return
    try:
        with exclusive_lock(settings.data_dir / SERVE_LOCK_NAME):
            upgrade_database(settings)
            _run_automatic_step(settings)
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
    except LockBusy:
        fail("omsync serve is already running, or a restore is in progress. Stop it first.")


def _serve_demo(settings: Settings) -> None:
    """Build the demo in a temporary directory and serve a copy of it. The directory, and every
    change made in the demo, is deleted when the server stops."""
    with tempfile.TemporaryDirectory(prefix="omsync-demo-") as tmp, _exit_on_terminate():
        root = Path(tmp)
        pristine = root / "pristine"
        typer.echo("Building the demo…")
        try:
            build_demo(pristine, today=date.today())
        except (RuntimeError, ValueError) as exc:
            fail(f"the demo could not be built: {exc}")
        shutil.copytree(pristine, root / "data")
        demo_settings = dataclasses.replace(settings, data_dir=root / "data", email=None)
        if not is_demo_dir(demo_settings.data_dir):
            fail("the demo copy is not a demo directory; refusing to serve it")
        site = DemoSite(pristine=pristine)
        uvicorn.run(
            create_app(demo_settings, vault=DemoVault(), demo=site),
            host=settings.host,
            port=settings.port,
        )


@contextmanager
def _exit_on_terminate() -> Iterator[None]:
    """Make SIGTERM unwind the stack while the demo runs. Uvicorn re-raises a signal it caught with
    the handler that was in place, and the default one ends the process without running cleanup,
    which would leave the demo's directory behind."""

    def stop(_signum: int, _frame: FrameType | None) -> None:
        raise SystemExit(0)

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _run_automatic_step(settings: Settings) -> None:
    """Run the automatic step once on the upgraded database. Never raises: a failure is logged."""
    engine = make_engine(database_url(settings))
    try:
        with Session(engine) as session:
            result = reconcile_after_import(session)
    except Exception:  # starting the server matters more than this step
        log.warning("The automatic step could not run at startup", exc_info=True)
        return
    finally:
        engine.dispose()
    if result.problem is not None:
        log.warning("The automatic step could not run at startup: %s", result.problem)
    elif result.categorized or result.identified or result.linked:
        typer.echo(
            f"Automatic step: categorized {result.categorized}, identified the other side of "
            f"{result.identified}, linked {result.linked}."
        )


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
