"""Alembic upgrade and downgrade on a temporary file database.

The URL is passed with `-x url=...`, as on the command line, so these tests never reach the
real data directory.
"""

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from omsync.core.db import Base, make_engine
from omsync.core.schema import MIGRATIONS_DIR

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.cmd_opts = Namespace(x=[f"url={url}"])
    return config


def _file_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migrations.db'}"


def test_migrated_schema_matches_the_models(tmp_path: Path) -> None:
    url = _file_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    finally:
        engine.dispose()

    assert differences == []


def test_a_deleted_row_id_is_never_reused(tmp_path: Path) -> None:
    url = _file_url(tmp_path)
    command.upgrade(_alembic_config(url), "head")

    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            for statement in (
                "INSERT INTO institution (id, key, name) VALUES (1, 'example', 'Example Bank')",
                "INSERT INTO account (id, institution_id, name, type) "
                "VALUES (1, 1, 'Checking', 'checking')",
                "INSERT INTO category (id, name, type) VALUES (1, 'Groceries', 'flexible')",
                'INSERT INTO "transaction" (id, account_id, posted_on, amount_cents, payee, '
                "payee_raw) VALUES (1, 1, '2026-09-12', -1580, 'Market', 'MARKET')",
                "INSERT INTO split (transaction_id, position, amount_cents, category_id) "
                "VALUES (1, 0, -1000, 1), (1, 1, -580, 1)",
                "DELETE FROM split WHERE id = 2",
                "INSERT INTO split (transaction_id, position, amount_cents, category_id) "
                "VALUES (1, 1, -580, 1)",
            ):
                connection.execute(text(statement))
            assert connection.execute(text("SELECT id FROM split ORDER BY id")).scalars().all() == [
                1,
                3,
            ]
    finally:
        engine.dispose()
