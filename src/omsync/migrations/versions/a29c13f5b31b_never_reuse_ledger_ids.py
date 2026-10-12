"""never reuse ledger ids

Every ledger table that batches insert into or delete from is rebuilt with AUTOINCREMENT, so
SQLite never hands a deleted row's id to a new row. Undo depends on this: a batch that deletes
a split and inserts another must not give the new one the old id, or undo cannot tell the rows
apart.

The transaction table was meant to have AUTOINCREMENT from the start, but the batch rebuild in
bb19056950f6 dropped it, because a batch rebuild does not carry sqlite_autoincrement over. This
revision restores it. Alembic's autogenerate does not compare table options, so the check is
in tests/ledger/test_migrations.py.

link.batch_id is dropped. The batch that inserted a link is recorded in batch_change, as it is
for every other row.

Tables are rebuilt with foreign keys off, as SQLite's documented table-rebuild procedure does:
a DROP TABLE on a parent checks its children while foreign keys are on. Alembic commits only
after this function returns, and SQLite ignores the pragma inside a transaction, so foreign keys
stay off on this connection. The run closes that connection. The foreign key check below runs
before it does.

The downgrade restores link.batch_id from batch_change, and removes AUTOINCREMENT by rebuilding
the tables without it.

Revision ID: a29c13f5b31b
Revises: 13d3611487da
Create Date: 2026-10-11 22:29:31.708780

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a29c13f5b31b"
down_revision: str | None = "13d3611487da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every ledger table with an integer surrogate key that batches write to. The core batch
# tables (batch, batch_change, data_revision) are not batch targets, so they are left alone.
AUTOINCREMENT_TABLES = (
    "account",
    "account_alias",
    "category",
    "connector_config",
    "counterparty",
    "import_review",
    "institution",
    "keyword",
    "link",
    "proposal",
    "rejected_proposal",
    "saved_filter",
    "split",
    "statement_balance",
    "statement_file",
    "statement_row",
    "sync_run",
    "transaction",
)


def upgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    for table in AUTOINCREMENT_TABLES:
        with op.batch_alter_table(
            table, recreate="always", table_kwargs={"sqlite_autoincrement": True}
        ) as batch_op:
            if table == "link":
                batch_op.drop_index("ix_link_batch_id")
                batch_op.drop_column("batch_id")
    _check_foreign_keys(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    # Add the column nullable, fill it from the batch that inserted each link, then tighten it.
    op.add_column("link", sa.Column("batch_id", sa.Integer(), nullable=True))
    bind.exec_driver_sql(
        "UPDATE link SET batch_id = ("
        "  SELECT batch_change.batch_id FROM batch_change"
        "  WHERE batch_change.table_name = 'link' AND batch_change.row_id = CAST(link.id AS TEXT)"
        "  ORDER BY batch_change.id LIMIT 1)"
    )
    missing = bind.exec_driver_sql("SELECT COUNT(*) FROM link WHERE batch_id IS NULL").scalar()
    if missing:
        raise RuntimeError(f"{missing} link row(s) have no batch_change record to restore from")
    with op.batch_alter_table("link", recreate="always") as batch_op:
        batch_op.alter_column("batch_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key("fk_link_batch_id_batch", "batch", ["batch_id"], ["id"])
        batch_op.create_index("ix_link_batch_id", ["batch_id"], unique=False)
    for table in AUTOINCREMENT_TABLES:
        if table != "link":
            with op.batch_alter_table(table, recreate="always"):
                pass
    _check_foreign_keys(bind)


def _set_foreign_keys(bind: sa.Connection, *, enabled: bool) -> None:
    """SQLite ignores this pragma inside a transaction, so confirm that it took effect."""
    bind.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
    current = bind.exec_driver_sql("PRAGMA foreign_keys").scalar()
    if bool(current) != enabled:
        raise RuntimeError("SQLite did not change foreign_keys; it is inside a transaction")


def _check_foreign_keys(bind: sa.Connection) -> None:
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"foreign key violations after rebuilding tables: {violations[:5]}")
