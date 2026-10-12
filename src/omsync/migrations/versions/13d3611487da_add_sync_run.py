"""add sync run

The sync log: one row per sync of an institution, with the connector used, its status, how many
files it fetched, and a short message. Rows are operational records, written directly rather
than as batches. The downgrade drops the table and its rows.

Revision ID: 13d3611487da
Revises: bb19056950f6
Create Date: 2026-10-11 22:06:30.730765

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "13d3611487da"
down_revision: str | None = "bb19056950f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_key", sa.String(), nullable=False),
        sa.Column("connector_key", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                "needs_user_action",
                name="sync_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("files_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("message", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("sync_run")
