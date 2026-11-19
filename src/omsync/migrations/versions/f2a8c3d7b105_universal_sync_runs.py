"""universal sync runs

Elevate SyncRun to a universal operational record supporting multiple sync kinds (bank, receipt).
Adds kind, target_key, source_key, and items_fetched, and makes institution_key nullable.

Revision ID: f2a8c3d7b105
Revises: c71f0d2a9e48
Create Date: 2026-11-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a8c3d7b105"
down_revision: str | None = "c71f0d2a9e48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_run", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.Enum(
                    "bank",
                    "receipt",
                    name="sync_kind",
                    native_enum=False,
                    create_constraint=True,
                ),
                server_default="bank",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("target_key", sa.String(), server_default="", nullable=False))
        batch_op.add_column(sa.Column("source_key", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("items_fetched", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.alter_column(
            "institution_key",
            existing_type=sa.String(),
            nullable=True,
        )
        batch_op.alter_column(
            "message",
            existing_type=sa.String(length=200),
            type_=sa.String(length=500),
            nullable=True,
        )

    op.execute(
        "UPDATE sync_run SET "
        "target_key = COALESCE(provider_key, institution_key, ''), "
        "source_key = institution_key, "
        "items_fetched = files_fetched "
        "WHERE target_key = ''"
    )


def downgrade() -> None:
    with op.batch_alter_table("sync_run", schema=None) as batch_op:
        batch_op.alter_column(
            "message",
            existing_type=sa.String(length=500),
            type_=sa.String(length=200),
            nullable=True,
        )
        batch_op.alter_column(
            "institution_key",
            existing_type=sa.String(),
            nullable=False,
        )
        batch_op.drop_column("items_fetched")
        batch_op.drop_column("source_key")
        batch_op.drop_column("target_key")
        batch_op.drop_column("kind")
