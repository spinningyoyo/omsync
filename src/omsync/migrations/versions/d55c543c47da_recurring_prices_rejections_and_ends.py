"""recurring series can end, change price, and take per-charge and per-due-date decisions

A series gains starts_on, the first due date its schedule is anchored to, and ended_on, the last
date it is due. starts_on is backfilled from next_due, else last_seen, else the day the row was
created. next_due and last_seen stop being stored and are read from the matched transactions.

SeriesStatus gains ended. The status CHECK constraint comes from the enum, so the table is rebuilt
to change it. The stored values suggested, confirmed, and dismissed do not change.

Three tables hold the user's decisions: recurring_price is a new amount from a date,
recurring_rejection is a charge that is not this bill, and recurring_paid_elsewhere is a due date
paid outside the ledger. None cascades from the series. Deleting a series deletes these rows first,
in the same batch, so undo can restore them.

Undo is refused for a batch written before this revision, since its snapshots name next_due and
last_seen, which the rebuilt table does not have.

The downgrade drops the three tables, so those decisions are lost. An ended series returns as
dismissed, so it produces no occurrences and detection still does not suggest it. next_due is set
from starts_on for the older code, and last_seen is cleared until a refresh computes it. A batch
written after the upgrade is refused by undo after the downgrade, since its snapshots name starts_on
and ended_on.

Revision ID: d55c543c47da
Revises: f2c8a5d1b906
Create Date: 2026-10-22 22:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d55c543c47da"
down_revision: str | None = "f2c8a5d1b906"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("suggested", "confirmed", "dismissed", "ended")
OLD_STATUSES = ("suggested", "confirmed", "dismissed")


def upgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    op.add_column("recurring_series", sa.Column("starts_on", sa.Date(), nullable=True))
    op.add_column("recurring_series", sa.Column("ended_on", sa.Date(), nullable=True))
    bind.exec_driver_sql(
        "UPDATE recurring_series SET starts_on = COALESCE(next_due, last_seen, date(created_at))"
    )
    with op.batch_alter_table(
        "recurring_series", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint("series_status", type_="check")
        batch_op.drop_column("next_due")
        batch_op.drop_column("last_seen")
        batch_op.alter_column("starts_on", existing_type=sa.Date(), nullable=False)
        batch_op.create_check_constraint("series_status", _in("status", STATUSES))

    op.create_table(
        "recurring_price",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["series_id"], ["recurring_series.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "series_id", "effective_on", name="uq_recurring_price_series_effective"
        ),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "recurring_rejection",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["series_id"], ["recurring_series.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transaction.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "series_id", "transaction_id", name="uq_recurring_rejection_series_transaction"
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_recurring_rejection_transaction_id",
        "recurring_rejection",
        ["transaction_id"],
        unique=False,
    )
    op.create_table(
        "recurring_paid_elsewhere",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["series_id"], ["recurring_series.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "due_on", name="uq_recurring_paid_elsewhere_series_due"),
        sqlite_autoincrement=True,
    )
    _check_foreign_keys(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    op.drop_table("recurring_paid_elsewhere")
    op.drop_index("ix_recurring_rejection_transaction_id", table_name="recurring_rejection")
    op.drop_table("recurring_rejection")
    op.drop_table("recurring_price")
    bind.exec_driver_sql("UPDATE recurring_series SET status = 'dismissed' WHERE status = 'ended'")
    op.add_column("recurring_series", sa.Column("next_due", sa.Date(), nullable=True))
    op.add_column("recurring_series", sa.Column("last_seen", sa.Date(), nullable=True))
    bind.exec_driver_sql("UPDATE recurring_series SET next_due = starts_on")
    with op.batch_alter_table(
        "recurring_series", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint("series_status", type_="check")
        batch_op.drop_column("starts_on")
        batch_op.drop_column("ended_on")
        batch_op.create_check_constraint("series_status", _in("status", OLD_STATUSES))
    _check_foreign_keys(bind)


def _in(column: str, values: tuple[str, ...]) -> str:
    """The CHECK expression SQLAlchemy writes for a non-native enum column."""
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


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
