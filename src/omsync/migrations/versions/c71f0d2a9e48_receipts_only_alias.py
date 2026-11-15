"""receipts-only alias

An alias can be a number that only receipts show, such as a phone wallet's card number. Sync never
asks the bank for it. Every existing alias is a number the bank shows.

Revision ID: c71f0d2a9e48
Revises: a3e7c5d9b142
Create Date: 2026-11-14 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c71f0d2a9e48"
down_revision: str | None = "a3e7c5d9b142"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("account_alias", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("receipts_only", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("account_alias", schema=None) as batch_op:
        batch_op.drop_column("receipts_only")
