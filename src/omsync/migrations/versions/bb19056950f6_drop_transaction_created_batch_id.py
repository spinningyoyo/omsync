"""drop transaction created_batch_id

batch_change already records which batch inserted each transaction, so the column was
redundant. The downgrade restores the column, its index, and its foreign key, empty.

Revision ID: bb19056950f6
Revises: 72503193df9f
Create Date: 2026-10-11 21:15:59.893101

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb19056950f6"
down_revision: str | None = "72503193df9f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_transaction_created_batch_id"))
        batch_op.drop_column("created_batch_id")


def downgrade() -> None:
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.add_column(sa.Column("created_batch_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_transaction_created_batch_id_batch", "batch", ["created_batch_id"], ["id"]
        )
        batch_op.create_index(
            batch_op.f("ix_transaction_created_batch_id"), ["created_batch_id"], unique=False
        )
