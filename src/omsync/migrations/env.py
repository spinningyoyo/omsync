"""Alembic environment. The database URL must be given: `-x url=...`, else sqlalchemy.url.

There is no fallback to omsync settings. Run by hand without a URL, alembic would otherwise open
(and create) the real data directory, which development must never touch. `omsync db upgrade`
always passes the URL of the configured database.
"""

from logging.config import fileConfig

from alembic import context

# Importing the models registers every table on Base.metadata for autogenerate.
import omsync.models  # noqa: F401
from omsync.core.db import Base, make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Return the database URL from the first override that is set."""
    overrides = context.get_x_argument(as_dictionary=True)
    if overrides.get("url"):
        return overrides["url"]
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    raise SystemExit(
        "No database URL. Pass one for a disposable database, for example\n"
        "  uv run alembic -x url=sqlite:////tmp/omsync-dev.db upgrade head\n"
        "To migrate your own database, use `omsync db upgrade`, which backs it up first."
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    engine = make_engine(_resolve_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
