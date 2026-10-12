"""add amount rules

An amount rule sends a payee keyword's small matches to another category: when the winning keyword
of its list assigns from_category and a transaction's absolute amount is below the limit, the
transaction takes to_category. The table uses AUTOINCREMENT, so SQLite never reuses a deleted id,
which undo depends on. The downgrade drops the table and its rules.

Revision ID: b7e2d94a1c58
Revises: a4d1e8b2c6f3
Create Date: 2026-10-17 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2d94a1c58"
down_revision: str | None = "a4d1e8b2c6f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "amount_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("list_key", sa.String(), nullable=False),
        sa.Column("from_category_id", sa.Integer(), nullable=False),
        sa.Column("below_cents", sa.Integer(), nullable=False),
        sa.Column("to_category_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("below_cents > 0", name="ck_amount_rule_limit_positive"),
        sa.CheckConstraint(
            "from_category_id != to_category_id", name="ck_amount_rule_categories_differ"
        ),
        sa.ForeignKeyConstraint(["from_category_id"], ["category.id"]),
        sa.ForeignKeyConstraint(["to_category_id"], ["category.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "list_key", "from_category_id", "below_cents", name="uq_amount_rule_list_from_limit"
        ),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("amount_rule")
