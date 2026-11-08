"""allow cancelled syncs

A sync can be cancelled from the Bank sync page, and the sync log records it as cancelled. The
status column has a CHECK constraint that lists its values, so SQLite rebuilds the sync_run table
with cancelled added, as 421166fb58e9 rebuilt the category table. No row changes. The table has no
foreign keys, so none need checking.

The downgrade records every cancelled run as failed, the status a cancelled sync would have had
before, then rebuilds the table with the previous list.

Revision ID: ee590cd33e1a
Revises: 5e2b9c4d7a13
Create Date: 2026-11-07 20:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ee590cd33e1a"
down_revision: str | None = "5e2b9c4d7a13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYNC_STATUSES = ("running", "succeeded", "failed", "needs_user_action", "cancelled")
PREVIOUS_SYNC_STATUSES = ("running", "succeeded", "failed", "needs_user_action")


def upgrade() -> None:
    _replace_status_check(SYNC_STATUSES)


def downgrade() -> None:
    op.execute("UPDATE sync_run SET status = 'failed' WHERE status = 'cancelled'")
    _replace_status_check(PREVIOUS_SYNC_STATUSES)


def _replace_status_check(values: tuple[str, ...]) -> None:
    """Rebuild the sync_run table with a CHECK constraint on its status column that allows values.
    A rebuild is the only way to change a constraint's text in SQLite."""
    with op.batch_alter_table(
        "sync_run", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint("sync_status", type_="check")
        batch_op.create_check_constraint("sync_status", _in("status", values))


def _in(column: str, values: tuple[str, ...]) -> str:
    """The CHECK expression SQLAlchemy writes for a non-native enum column."""
    return f"{column} IN ({', '.join(repr(value) for value in values)})"
