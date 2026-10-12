"""add plan tables

The Plan tables: monthly budget lines, goals, and recurring series (see omsync.ledger.plan_models).
Budget mode and expected income need no table: they are preferences. Every table uses AUTOINCREMENT,
so SQLite never reuses a deleted id, which undo depends on. The downgrade drops the three tables and
their rows.

Revision ID: 4cb3bb1205c6
Revises: 1682eaac13ef
Create Date: 2026-10-12 00:05:28.210469

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4cb3bb1205c6"
down_revision: str | None = "1682eaac13ef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "budget_line",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "category",
                "flex_pool",
                name="budget_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("rollover", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "(kind = 'flex_pool' AND category_id IS NULL)"
            " OR (kind = 'category' AND category_id IS NOT NULL)",
            name="ck_budget_line_category_matches_kind",
        ),
        sa.CheckConstraint("strftime('%d', month) = '01'", name="ck_budget_line_first_of_month"),
        sa.CheckConstraint("amount_cents >= 0", name="ck_budget_line_amount_nonnegative"),
        sa.ForeignKeyConstraint(["category_id"], ["category.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "month", "kind", "category_id", name="uq_budget_line_month_kind_category"
        ),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("budget_line", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_budget_line_category_id"), ["category_id"], unique=False
        )
        # NULLs are distinct in a unique constraint, so one flex pool line per month needs this.
        batch_op.create_index(
            "uq_budget_line_flex_pool_month",
            ["month"],
            unique=True,
            sqlite_where=sa.text("kind = 'flex_pool'"),
        )

    op.create_table(
        "goal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("target_cents", sa.Integer(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("linked_account_id", sa.Integer(), nullable=True),
        sa.Column("monthly_contribution_cents", sa.Integer(), nullable=False),
        sa.Column("color", sa.String(), nullable=True),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint("target_cents >= 0", name="ck_goal_target_nonnegative"),
        sa.CheckConstraint(
            "monthly_contribution_cents >= 0", name="ck_goal_contribution_nonnegative"
        ),
        sa.ForeignKeyConstraint(["linked_account_id"], ["account.id"]),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("goal", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_goal_linked_account_id"), ["linked_account_id"], unique=False
        )

    op.create_table(
        "recurring_series",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("payee_pattern", sa.String(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column(
            "cadence",
            sa.Enum(
                "weekly",
                "biweekly",
                "monthly",
                "quarterly",
                "yearly",
                name="cadence",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("amount_tolerance_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_due", sa.Date(), nullable=True),
        sa.Column("last_seen", sa.Date(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "suggested",
                "confirmed",
                "dismissed",
                name="series_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_tolerance_cents >= 0", name="ck_recurring_series_tolerance_nonnegative"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"]),
        sa.ForeignKeyConstraint(["category_id"], ["category.id"]),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("recurring_series", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_recurring_series_account_id"), ["account_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_recurring_series_category_id"), ["category_id"], unique=False
        )


def downgrade() -> None:
    op.drop_table("recurring_series")
    op.drop_table("goal")
    op.drop_table("budget_line")
