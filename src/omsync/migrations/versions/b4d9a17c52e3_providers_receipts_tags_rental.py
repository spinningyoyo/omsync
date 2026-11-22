"""receipt providers, sync runs of a provider, receipts, tags, and rental properties

Provider gains a kind and a name, and its institution becomes optional (a receipt provider has
none). A sync run now belongs to a provider. Receipts and tags get their tables. An account gets
its rental property label, and a category says whether it counts as a rental expense. The two
rental preferences are carried over and removed: the account IDs with their labels, and the
category names, or the default names when none were saved.

Revision ID: b4d9a17c52e3
Revises: c71f0d2a9e48
Create Date: 2026-11-19 12:00:00.000000

"""

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d9a17c52e3"
down_revision: str | None = "c71f0d2a9e48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_RENTAL_CATEGORIES = ("Supply", "Repair", "Utility", "Operations")


def upgrade() -> None:
    _rebuild_provider()
    _recreate_sync_run()
    _create_receipts()
    _create_tags()
    _add_rental()


def _rebuild_provider() -> None:
    with _links_set_aside():
        with op.batch_alter_table("provider", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "kind",
                    sa.Enum(
                        "bank",
                        "receipt",
                        name="provider_kind",
                        native_enum=False,
                        create_constraint=True,
                    ),
                    server_default="bank",
                    nullable=False,
                )
            )
            batch_op.add_column(sa.Column("name", sa.String(), server_default="", nullable=False))
            batch_op.alter_column("institution_id", existing_type=sa.Integer(), nullable=True)
        with op.batch_alter_table("provider", schema=None) as batch_op:
            batch_op.alter_column("kind", existing_type=sa.String(), server_default=None)
            batch_op.alter_column("name", existing_type=sa.String(), server_default=None)


@contextmanager
def _links_set_aside() -> Iterator[None]:
    """Rebuilding provider drops it, which SQLite refuses while provider_account rows point at it.
    The rows wait in a temporary table meanwhile."""
    op.execute("CREATE TEMP TABLE provider_account_aside AS SELECT * FROM provider_account")
    op.execute("DELETE FROM provider_account")
    yield
    op.execute("INSERT INTO provider_account SELECT * FROM provider_account_aside")
    op.execute("DROP TABLE provider_account_aside")


def _recreate_sync_run() -> None:
    op.drop_table("sync_run")
    op.create_table(
        "sync_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
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
        sa.Column("items_fetched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["provider_id"], ["provider.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("sync_run", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_run_provider_id"), ["provider_id"], unique=False)


def _create_receipts() -> None:
    op.create_table(
        "receipt",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("vendor_key", sa.String(length=32), nullable=False),
        sa.Column("receipt_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.String(length=64), nullable=False),
        sa.Column("purchased_on", sa.Date(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("tax_cents", sa.Integer(), nullable=False),
        sa.Column("is_refund", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("refund_of", sa.String(length=128), nullable=True),
        sa.Column("ship_dates", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("stored_path", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["provider.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vendor_key", "receipt_id", name="uq_receipt_vendor_id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("receipt", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_receipt_provider_id"), ["provider_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_purchased_on"), ["purchased_on"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_receipt_id"), ["receipt_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_vendor_key"), ["vendor_key"], unique=False)

    op.create_table(
        "receipt_item",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("taxable", sa.Boolean(), nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("code", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipt.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_id", "position", name="uq_receipt_item_position"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("receipt_item", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_receipt_item_receipt_id"), ["receipt_id"], unique=False
        )

    op.create_table(
        "receipt_tender",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("last4", sa.String(length=4), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipt.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("receipt_tender", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_receipt_tender_receipt_id"), ["receipt_id"], unique=False
        )


def _create_tags() -> None:
    op.create_table(
        "tag",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("color", sa.String(length=7), server_default="#737373", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("tag", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tag_name"), ["name"], unique=True)

    op.create_table(
        "transaction_tag",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transaction.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id", "tag_id", name="uq_transaction_tag"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("transaction_tag", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_transaction_tag_tag_id"), ["tag_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_transaction_tag_transaction_id"), ["transaction_id"], unique=False
        )

    op.create_table(
        "split_tag",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("split_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["split_id"], ["split.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("split_id", "tag_id", name="uq_split_tag"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("split_tag", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_split_tag_split_id"), ["split_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_split_tag_tag_id"), ["tag_id"], unique=False)


def _add_rental() -> None:
    # Plain ADD COLUMN: a batch rebuild would drop tables that other tables reference.
    op.add_column("account", sa.Column("rental_label", sa.String(), nullable=True))
    op.add_column(
        "category", sa.Column("rental", sa.Boolean(), server_default=sa.false(), nullable=False)
    )

    bind = op.get_bind()
    stored = dict(bind.execute(sa.text("SELECT key, value FROM preference")).all())
    accounts = _json(stored.get("rental_accounts")) or {}
    for account_id, label in accounts.items():
        bind.execute(
            sa.text("UPDATE account SET rental_label = :label WHERE id = :id"),
            {"label": str(label).strip(), "id": int(account_id)},
        )
    names = _json(stored.get("rental_categories"))
    for name in DEFAULT_RENTAL_CATEGORIES if names is None else names:
        bind.execute(
            sa.text("UPDATE category SET rental = 1 WHERE name = :name"),
            {"name": str(name).strip()},
        )
    bind.execute(
        sa.text("DELETE FROM preference WHERE key IN ('rental_accounts', 'rental_categories')")
    )


def _json(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def downgrade() -> None:
    op.drop_column("category", "rental")
    op.drop_column("account", "rental_label")
    op.drop_table("split_tag")
    op.drop_table("transaction_tag")
    op.drop_table("tag")
    op.drop_table("receipt_tender")
    op.drop_table("receipt_item")
    op.drop_table("receipt")
    op.drop_table("sync_run")
    with _links_set_aside():
        op.execute(
            "DELETE FROM provider_account_aside "
            "WHERE provider_id IN (SELECT id FROM provider WHERE kind = 'receipt')"
        )
        op.execute("DELETE FROM provider WHERE kind = 'receipt'")
        with op.batch_alter_table("provider", schema=None) as batch_op:
            batch_op.drop_constraint("provider_kind", type_="check")
            batch_op.drop_column("name")
            batch_op.drop_column("kind")
            batch_op.alter_column("institution_id", existing_type=sa.Integer(), nullable=False)
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
