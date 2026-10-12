"""SQLAlchemy engine, session factory, and the declarative base for omsync tables."""

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from omsync.core.settings import Settings


class Base(DeclarativeBase):
    """Declarative base for every omsync table."""


def make_engine(url: str) -> Engine:
    """Create an engine whose SQLite connections enable foreign keys.

    File databases also use WAL journaling. In-memory databases cannot, so they skip it.
    """
    engine = create_engine(url)
    in_memory = _is_memory(url)

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            if not in_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to the engine."""
    return sessionmaker(bind=engine)


def database_url(settings: Settings) -> str:
    """Return the SQLite URL for the omsync database under the data directory."""
    return f"sqlite:///{settings.db_path}"


def _is_memory(url: str) -> bool:
    parsed = make_url(url)
    return parsed.database in (None, "", ":memory:") or parsed.query.get("mode") == "memory"
