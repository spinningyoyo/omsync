"""add preference

View settings, such as the reports' joint share toggle: one row per key, holding a JSON value.
Preferences are written directly rather than as batches (see omsync.ledger.preferences). The
downgrade drops the table and its rows.

Revision ID: 1682eaac13ef
Revises: a29c13f5b31b
Create Date: 2026-10-11 23:40:44.614135

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1682eaac13ef"
down_revision: str | None = "a29c13f5b31b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "preference",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("preference")
