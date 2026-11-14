"""initial schema

Creates every table of the ledger. Tables that batches write to use AUTOINCREMENT, so SQLite never
gives a deleted row's id to a new row, and undo can tell the rows apart.

Revision ID: a3e7c5d9b142
Revises:
Create Date: 2026-11-13 20:23:35.962400

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3e7c5d9b142"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "batch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "previewed",
                "applied",
                "undone",
                "failed",
                name="batch_state",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="previewed",
            nullable=False,
        ),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_table(
        "category",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "fixed",
                "flexible",
                "non_monthly",
                "unassigned",
                "income",
                "excluded",
                "owed",
                name="category_type",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="unassigned",
            nullable=False,
        ),
        sa.Column("hidden", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("icon", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "contact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "person", "business", name="contact_kind", native_enum=False, create_constraint=True
            ),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "co_owner",
                "other",
                name="contact_role",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="other",
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("contact", schema=None) as batch_op:
        batch_op.create_index(
            "uq_contact_one_owner", ["role"], unique=True, sqlite_where=sa.text("role = 'owner'")
        )

    op.create_table(
        "data_revision",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("value", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("id = 1", name="ck_data_revision_single_row"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "institution",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "preference",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "rejected_proposal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("signature", sa.String(), nullable=False),
        sa.Column(
            "rejected_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("rule", sa.String(), nullable=True),
        sa.Column(
            "kind",
            sa.Enum(
                "transfer",
                "receipt",
                "refund",
                "p2p",
                "joint_settlement",
                name="link_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column(
            "reason",
            sa.Enum(
                "rejected",
                "unlinked",
                name="rejection_reason",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="rejected",
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("signature"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "saved_filter",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "sync_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_key", sa.String(), nullable=False),
        sa.Column("provider_key", sa.String(), nullable=True),
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
                "cancelled",
                name="sync_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("files_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("message", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "checking",
                "savings",
                "credit_card",
                "brokerage",
                "other",
                name="account_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("last4", sa.String(length=4), nullable=True),
        sa.Column("joint", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("closed", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institution.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_account_institution_id"), ["institution_id"], unique=False
        )

    op.create_table(
        "batch_change",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("table_name", sa.String(), nullable=False),
        sa.Column("row_id", sa.String(), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["batch_id"], ["batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("batch_change", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_batch_change_batch_id"), ["batch_id"], unique=False)

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
            "(kind = 'flex_pool' AND category_id IS NULL) "
            "OR (kind = 'category' AND category_id IS NOT NULL)",
            name="ck_budget_line_category_matches_kind",
        ),
        sa.CheckConstraint("strftime('%d', month) = '01'", name="ck_budget_line_first_of_month"),
        sa.CheckConstraint("amount_cents >= 0", name="ck_budget_line_amount_nonnegative"),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "month", "kind", "category_id", name="uq_budget_line_month_kind_category"
        ),
        info={"label": "budget line"},
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("budget_line", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_budget_line_category_id"), ["category_id"], unique=False
        )
        batch_op.create_index(
            "uq_budget_line_flex_pool_month",
            ["month"],
            unique=True,
            sqlite_where=sa.text("kind = 'flex_pool'"),
        )

    op.create_table(
        "contact_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("last4", sa.String(), nullable=True),
        sa.CheckConstraint(
            "last4 IS NULL OR (length(last4) = 4 AND last4 NOT GLOB '*[^0-9]*')",
            name="ck_contact_account_last4",
        ),
        sa.ForeignKeyConstraint(["contact_id"], ["contact.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("contact_account", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_contact_account_contact_id"), ["contact_id"], unique=False
        )

    op.create_table(
        "contact_name",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("given", sa.String(), nullable=False),
        sa.Column("middle", sa.String(), nullable=True),
        sa.Column("family", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["contact_id"], ["contact.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contact_id", "position", name="uq_contact_name_contact_position"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("contact_name", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_contact_name_contact_id"), ["contact_id"], unique=False
        )

    op.create_table(
        "keyword",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("list_key", sa.String(), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
        sa.Column("is_regex", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("list_key", "pattern", name="uq_keyword_list_pattern"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("keyword", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_keyword_category_id"), ["category_id"], unique=False)

    op.create_table(
        "provider",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("connector_key", sa.String(), nullable=False),
        sa.Column("vault_item_id", sa.String(), nullable=True),
        sa.Column("settings", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institution.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("provider", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_provider_institution_id"), ["institution_id"], unique=False
        )

    op.create_table(
        "account_alias",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("statement_account_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institution.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "institution_id",
            "statement_account_id",
            name="uq_account_alias_institution_statement_account",
        ),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("account_alias", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_account_alias_account_id"), ["account_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_account_alias_institution_id"), ["institution_id"], unique=False
        )

    op.create_table(
        "account_nickname",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("nickname", sa.String(collation="NOCASE"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "nickname", name="uq_account_nickname_account_nickname"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("account_nickname", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_account_nickname_account_id"), ["account_id"], unique=False
        )

    op.create_table(
        "category_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("paid_by", sa.String(), nullable=True),
        sa.Column("below_cents", sa.Integer(), nullable=True),
        sa.Column("at_least_cents", sa.Integer(), nullable=True),
        sa.Column(
            "counterparty",
            sa.Enum(
                "person",
                "contact",
                "owner_account",
                name="rule_counterparty",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("to_category_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "CASE WHEN counterparty = 'contact' THEN contact_id IS NOT NULL "
            "ELSE contact_id IS NULL END",
            name="ck_category_rule_contact",
        ),
        sa.CheckConstraint(
            "at_least_cents IS NULL OR at_least_cents > 0", name="ck_category_rule_at_least"
        ),
        sa.CheckConstraint(
            "below_cents IS NULL OR at_least_cents IS NULL OR below_cents > at_least_cents",
            name="ck_category_rule_range",
        ),
        sa.CheckConstraint("below_cents IS NULL OR below_cents > 0", name="ck_category_rule_below"),
        sa.CheckConstraint(
            "paid_by IS NOT NULL OR below_cents IS NOT NULL OR at_least_cents IS NOT NULL "
            "OR counterparty IS NOT NULL OR account_id IS NOT NULL",
            name="ck_category_rule_has_condition",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contact.id"],
        ),
        sa.ForeignKeyConstraint(
            ["to_category_id"],
            ["category.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "contact_pattern",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
        sa.Column("contact_account_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("side IN ('out', 'in')", name="ck_contact_pattern_side"),
        sa.ForeignKeyConstraint(
            ["contact_account_id"],
            ["contact_account.id"],
        ),
        sa.ForeignKeyConstraint(["contact_id"], ["contact.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "contact_id", "side", "pattern", name="uq_contact_pattern_side_pattern"
        ),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("contact_pattern", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_contact_pattern_contact_id"), ["contact_id"], unique=False
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
        sa.CheckConstraint(
            "monthly_contribution_cents >= 0", name="ck_goal_contribution_nonnegative"
        ),
        sa.CheckConstraint("target_cents >= 0", name="ck_goal_target_nonnegative"),
        sa.ForeignKeyConstraint(
            ["linked_account_id"],
            ["account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("goal", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_goal_linked_account_id"), ["linked_account_id"], unique=False
        )

    op.create_table(
        "provider_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("provider_account", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_provider_account_account_id"), ["account_id"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_provider_account_provider_id"), ["provider_id"], unique=False
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
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "suggested",
                "confirmed",
                "dismissed",
                "ended",
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
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
        ),
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

    op.create_table(
        "statement_file",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("format_key", sa.String(), nullable=False),
        sa.Column("original_name", sa.String(), nullable=False),
        sa.Column("stored_path", sa.String(), nullable=False),
        sa.Column("statement_account_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("statement_file", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_statement_file_account_id"), ["account_id"], unique=False
        )

    op.create_table(
        "transaction",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("posted_on", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("payee", sa.String(), nullable=False),
        sa.Column("payee_raw", sa.String(), nullable=False),
        sa.Column("memo", sa.String(), nullable=True),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column(
            "category_origin",
            sa.Enum(
                "none",
                "keyword",
                "rule",
                "receipt",
                "manual",
                name="category_origin",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="none",
            nullable=False,
        ),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("contact_account_id", sa.Integer(), nullable=True),
        sa.Column(
            "contact_origin",
            sa.Enum(
                "none",
                "recognized",
                "manual",
                name="contact_origin",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="none",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
        ),
        sa.ForeignKeyConstraint(
            ["contact_account_id"],
            ["contact_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contact.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_transaction_account_id"), ["account_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_transaction_category_id"), ["category_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_transaction_contact_id"), ["contact_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_transaction_posted_on"), ["posted_on"], unique=False)

    op.create_table(
        "link",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "transfer",
                "receipt",
                "refund",
                "p2p",
                "joint_settlement",
                name="link_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("from_transaction_id", sa.Integer(), nullable=True),
        sa.Column("to_transaction_id", sa.Integer(), nullable=True),
        sa.Column("receipt_key", sa.String(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_link_amount_positive"),
        sa.ForeignKeyConstraint(
            ["from_transaction_id"],
            ["transaction.id"],
        ),
        sa.ForeignKeyConstraint(
            ["to_transaction_id"],
            ["transaction.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("link", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_link_from_transaction_id"), ["from_transaction_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_link_to_transaction_id"), ["to_transaction_id"], unique=False
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
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["recurring_series.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series_id", "due_on", name="uq_recurring_paid_elsewhere_series_due"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "recurring_price",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["recurring_series.id"],
        ),
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
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["recurring_series.id"],
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transaction.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "series_id", "transaction_id", name="uq_recurring_rejection_series_transaction"
        ),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("recurring_rejection", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_recurring_rejection_transaction_id"), ["transaction_id"], unique=False
        )

    op.create_table(
        "split",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=True),
        sa.Column(
            "category_origin",
            sa.Enum(
                "none",
                "keyword",
                "rule",
                "receipt",
                "manual",
                name="category_origin",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="none",
            nullable=False,
        ),
        sa.Column("memo", sa.String(), nullable=True),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("contact_account_id", sa.Integer(), nullable=True),
        sa.Column(
            "contact_origin",
            sa.Enum(
                "none",
                "recognized",
                "manual",
                name="contact_origin",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="none",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
        ),
        sa.ForeignKeyConstraint(
            ["contact_account_id"],
            ["contact_account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contact.id"],
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transaction.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id", "position", name="uq_split_transaction_position"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("split", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_split_category_id"), ["category_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_split_contact_id"), ["contact_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_split_transaction_id"), ["transaction_id"], unique=False
        )

    op.create_table(
        "statement_balance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("statement_file_id", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("balance_cents", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "ledger",
                "available",
                name="balance_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
        ),
        sa.ForeignKeyConstraint(
            ["statement_file_id"],
            ["statement_file.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("statement_balance", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_statement_balance_account_id"), ["account_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_statement_balance_statement_file_id"),
            ["statement_file_id"],
            unique=False,
        )

    op.create_table(
        "statement_row",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("statement_file_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("posted_on", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("payee_raw", sa.String(), nullable=False),
        sa.Column("memo_raw", sa.String(), nullable=True),
        sa.Column("fitid", sa.String(), nullable=True),
        sa.Column("fitid_trusted", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["statement_file_id"],
            ["statement_file.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("statement_file_id", "position", name="uq_statement_row_file_position"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("statement_row", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_statement_row_fingerprint"), ["fingerprint"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_statement_row_fitid"), ["fitid"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_statement_row_statement_file_id"), ["statement_file_id"], unique=False
        )

    op.create_table(
        "import_review",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("statement_row_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("candidate_transaction_ids", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open", "resolved", name="review_status", native_enum=False, create_constraint=True
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column("resolved_transaction_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["resolved_transaction_id"],
            ["transaction.id"],
        ),
        sa.ForeignKeyConstraint(
            ["statement_row_id"],
            ["statement_row.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("import_review", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_import_review_resolved_transaction_id"),
            ["resolved_transaction_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_import_review_statement_row_id"), ["statement_row_id"], unique=True
        )

    op.create_table(
        "transaction_row",
        sa.Column("statement_row_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column(
            "match_kind",
            sa.Enum(
                "created",
                "fitid",
                "fingerprint",
                "manual",
                name="match_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["statement_row_id"],
            ["statement_row.id"],
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transaction.id"],
        ),
        sa.PrimaryKeyConstraint("statement_row_id"),
    )
    with op.batch_alter_table("transaction_row", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_transaction_row_transaction_id"), ["transaction_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("transaction_row", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_transaction_row_transaction_id"))

    op.drop_table("transaction_row")
    with op.batch_alter_table("import_review", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_import_review_statement_row_id"))
        batch_op.drop_index(batch_op.f("ix_import_review_resolved_transaction_id"))

    op.drop_table("import_review")
    with op.batch_alter_table("statement_row", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_statement_row_statement_file_id"))
        batch_op.drop_index(batch_op.f("ix_statement_row_fitid"))
        batch_op.drop_index(batch_op.f("ix_statement_row_fingerprint"))

    op.drop_table("statement_row")
    with op.batch_alter_table("statement_balance", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_statement_balance_statement_file_id"))
        batch_op.drop_index(batch_op.f("ix_statement_balance_account_id"))

    op.drop_table("statement_balance")
    with op.batch_alter_table("split", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_split_transaction_id"))
        batch_op.drop_index(batch_op.f("ix_split_contact_id"))
        batch_op.drop_index(batch_op.f("ix_split_category_id"))

    op.drop_table("split")
    with op.batch_alter_table("recurring_rejection", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_recurring_rejection_transaction_id"))

    op.drop_table("recurring_rejection")
    op.drop_table("recurring_price")
    op.drop_table("recurring_paid_elsewhere")
    with op.batch_alter_table("link", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_link_to_transaction_id"))
        batch_op.drop_index(batch_op.f("ix_link_from_transaction_id"))

    op.drop_table("link")
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_transaction_posted_on"))
        batch_op.drop_index(batch_op.f("ix_transaction_contact_id"))
        batch_op.drop_index(batch_op.f("ix_transaction_category_id"))
        batch_op.drop_index(batch_op.f("ix_transaction_account_id"))

    op.drop_table("transaction")
    with op.batch_alter_table("statement_file", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_statement_file_account_id"))

    op.drop_table("statement_file")
    with op.batch_alter_table("recurring_series", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_recurring_series_category_id"))
        batch_op.drop_index(batch_op.f("ix_recurring_series_account_id"))

    op.drop_table("recurring_series")
    with op.batch_alter_table("provider_account", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_provider_account_provider_id"))
        batch_op.drop_index(batch_op.f("ix_provider_account_account_id"))

    op.drop_table("provider_account")
    with op.batch_alter_table("goal", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_goal_linked_account_id"))

    op.drop_table("goal")
    with op.batch_alter_table("contact_pattern", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_contact_pattern_contact_id"))

    op.drop_table("contact_pattern")
    op.drop_table("category_rule")
    with op.batch_alter_table("account_nickname", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_account_nickname_account_id"))

    op.drop_table("account_nickname")
    with op.batch_alter_table("account_alias", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_account_alias_institution_id"))
        batch_op.drop_index(batch_op.f("ix_account_alias_account_id"))

    op.drop_table("account_alias")
    with op.batch_alter_table("provider", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_provider_institution_id"))

    op.drop_table("provider")
    with op.batch_alter_table("keyword", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_keyword_category_id"))

    op.drop_table("keyword")
    with op.batch_alter_table("contact_name", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_contact_name_contact_id"))

    op.drop_table("contact_name")
    with op.batch_alter_table("contact_account", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_contact_account_contact_id"))

    op.drop_table("contact_account")
    with op.batch_alter_table("budget_line", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_budget_line_flex_pool_month", sqlite_where=sa.text("kind = 'flex_pool'")
        )
        batch_op.drop_index(batch_op.f("ix_budget_line_category_id"))

    op.drop_table("budget_line")
    with op.batch_alter_table("batch_change", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_batch_change_batch_id"))

    op.drop_table("batch_change")
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_account_institution_id"))

    op.drop_table("account")
    op.drop_table("sync_run")
    op.drop_table("saved_filter")
    op.drop_table("rejected_proposal")
    op.drop_table("preference")
    op.drop_table("institution")
    op.drop_table("data_revision")
    with op.batch_alter_table("contact", schema=None) as batch_op:
        batch_op.drop_index("uq_contact_one_owner", sqlite_where=sa.text("role = 'owner'"))

    op.drop_table("contact")
    op.drop_table("category")
    op.drop_table("batch")
