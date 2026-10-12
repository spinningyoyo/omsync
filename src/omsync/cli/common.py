"""Helpers the omsync commands share: settings, the database at the head revision, and errors.

Commands that touch the ledger open the database through ready_session. It refuses a missing or
out-of-date database and never migrates; `omsync db upgrade` is the only command that does.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import NoReturn

import typer
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from omsync.core.db import database_url, make_engine
from omsync.core.schema import head_revision
from omsync.core.settings import Settings, load_settings


def load_settings_or_exit() -> Settings:
    """Load settings, or exit 1 with the reason if the config file is invalid."""
    try:
        return load_settings()
    except ValueError as exc:
        fail(f"config file: {exc}")


def parse_date(option: str, raw: str) -> date:
    """A YYYY-MM-DD option value, or exit 1 naming the option."""
    try:
        return date.fromisoformat(raw)
    except ValueError:
        fail(f"{option} must be a date like 2026-10-01")


def fail(message: str) -> NoReturn:
    """Print an error to stderr and exit 1."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=1)


def parse_period(
    start: str | None, end: str | None, *, start_flag: str, end_flag: str
) -> tuple[date, date] | None:
    """Two ISO dates given together, inclusive, or None when neither is given. Exits 1 otherwise."""
    if start is None and end is None:
        return None
    if start is None or end is None:
        fail(f"give {start_flag} and {end_flag} together, or neither")
    first = parse_date(start_flag, start)
    last = parse_date(end_flag, end)
    if first > last:
        fail(f"{start_flag} is after {end_flag}")
    return first, last


@contextmanager
def ready_session() -> Iterator[tuple[Settings, Session]]:
    """Yield settings and a session on the database, which must be at the head revision.

    Exits 1 with the upgrade hint if the database is missing or behind. The engine is disposed on
    exit, so no connection is left holding the file open.
    """
    settings = load_settings_or_exit()
    if not settings.db_path.is_file():
        fail(f"there is no database at {settings.db_path}. Run `omsync db upgrade` first.")
    engine = make_engine(database_url(settings))
    try:
        current = _current_revision(engine)
        head = head_revision()
        if current != head:
            fail(
                f"the database is at revision {current or 'none'}, but the head is {head}. "
                "Run `omsync db upgrade` first."
            )
        with Session(engine) as session:
            yield settings, session
    finally:
        engine.dispose()


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()
