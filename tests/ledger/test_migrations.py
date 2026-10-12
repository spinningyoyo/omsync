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


PREVIOUS_HEAD = "13d3611487da"


def _seed_previous_head(url: str) -> None:
    """One institution, account, four categories, a transaction with two splits, and one link made
    by a batch, written at the schema before this revision."""
    statements = [
        "INSERT INTO institution (id, key, name) VALUES (1, 'example-bank', 'Example Bank')",
        "INSERT INTO account (id, institution_id, name, kind, last4, joint, closed) "
        "VALUES (1, 1, 'Checking', 'checking', '1234', 0, 0)",
        'INSERT INTO category (id, name, "group", flex_type, is_income, hidden) VALUES '
        "(1, 'Groceries', 'Need', 'none', 0, 0), (2, 'Pay', 'Income', 'none', 1, 0), "
        "(3, 'Transfer', 'ignore', 'none', 0, 0), (4, 'Rent', 'Need', 'fixed', 0, 0)",
        'INSERT INTO "transaction" (id, account_id, posted_on, amount_cents, payee, payee_raw, '
        "category_origin) VALUES (1, 1, '2026-09-12', -1580, 'Market', 'MARKET', 'none')",
        "INSERT INTO split (id, transaction_id, position, amount_cents, category_id, "
        "category_origin) VALUES (1, 1, 0, -1000, 1, 'keyword')",
        "INSERT INTO split (id, transaction_id, position, amount_cents, category_id, "
        "category_origin) VALUES (2, 1, 1, -580, 1, 'keyword')",
        "INSERT INTO batch (id, kind, state, base_revision, idempotency_key) "
        "VALUES (1, 'receipts', 'applied', 0, 'seed-1')",
        "INSERT INTO batch_change (id, batch_id, table_name, row_id, before, after) "
        "VALUES (1, 1, 'link', '1', NULL, '{}')",
        "INSERT INTO link (id, kind, amount_cents, to_transaction_id, receipt_key, evidence, "
        "batch_id) VALUES (1, 'receipt', -1580, 1, 'fake:R1:0', '{}', 1)",
    ]
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def test_upgrade_keeps_rows_and_ids_and_stops_reusing_them(tmp_path: Path) -> None:
    url = _file_url(tmp_path)
    config = _alembic_config(url)
    command.upgrade(config, PREVIOUS_HEAD)
    _seed_previous_head(url)

    command.upgrade(config, "head")

    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            assert connection.execute(text("SELECT id FROM split ORDER BY id")).scalars().all() == [
                1,
                2,
            ]
            assert connection.execute(text("SELECT id FROM link")).scalars().all() == [1]
            # Group, income flag, and flex type become one type. An Ignore group is excluded.
            assert connection.execute(text("SELECT id, type FROM category ORDER BY id")).all() == [
                (1, "unassigned"),
                (2, "income"),
                (3, "excluded"),
                (4, "fixed"),
            ]
            link_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(link)"))}
            assert "batch_id" not in link_columns
            # Delete the highest split, then insert: SQLite must not hand its id back out.
            connection.execute(text("DELETE FROM split WHERE id = 2"))
            connection.execute(
                text(
                    "INSERT INTO split (transaction_id, position, amount_cents, category_id, "
                    "category_origin) VALUES (1, 2, -580, 1, 'receipt')"
                )
            )
            assert connection.execute(text("SELECT MAX(id) FROM split")).scalar() == 3
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()
