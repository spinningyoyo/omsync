"""add providers

A provider is how omsync fetches an institution's statements: a bank connector and its vault login.
It replaces connector_config, which allowed one per institution. An institution may now have several
providers, and provider_account names the accounts each one fetches. An account has at most one.

Each connector_config row becomes a provider with the institution's key, keeping its id, so existing
ledger references and the sync history stay meaningful. The provider owns every account of its
institution. sync_run gains provider_key, filled from institution_key for every existing run.

The downgrade restores connector_config. It refuses when an institution has more than one provider,
because connector_config holds one row per institution and cannot say which provider to keep.

Revision ID: dc76ac8ff6c6
Revises: c3a9d41e7b08
Create Date: 2026-10-14 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dc76ac8ff6c6"
down_revision: str | None = "c3a9d41e7b08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("connector_key", sa.String(), nullable=False),
        sa.Column("vault_item_id", sa.String(), nullable=True),
        sa.Column("settings", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(["institution_id"], ["institution.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("provider", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_provider_institution_id"), ["institution_id"], unique=False
        )

    op.create_table(
        "provider_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["provider.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"]),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("provider_account", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_provider_account_provider_id"), ["provider_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_provider_account_account_id"), ["account_id"], unique=True
        )

    op.execute(
        "INSERT INTO provider "
        "(id, key, institution_id, connector_key, vault_item_id, settings, enabled) "
        "SELECT connector_config.id, institution.key, connector_config.institution_id, "
        "connector_config.connector_key, connector_config.vault_item_id, "
        "connector_config.settings, connector_config.enabled "
        "FROM connector_config JOIN institution ON institution.id = connector_config.institution_id"
    )
    op.execute(
        "INSERT INTO provider_account (provider_id, account_id) "
        "SELECT provider.id, account.id FROM provider "
        "JOIN account ON account.institution_id = provider.institution_id"
    )

    op.add_column("sync_run", sa.Column("provider_key", sa.String(), nullable=True))
    op.execute("UPDATE sync_run SET provider_key = institution_key")

    with op.batch_alter_table("connector_config", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_connector_config_institution_id"))
    op.drop_table("connector_config")


def downgrade() -> None:
    bind = op.get_bind()
    shared = bind.exec_driver_sql(
        "SELECT institution_id FROM provider GROUP BY institution_id HAVING COUNT(*) > 1"
    ).fetchall()
    if shared:
        raise RuntimeError(
            f"{len(shared)} institution(s) have more than one provider, which connector_config "
            "cannot hold. Delete the extra providers before downgrading."
        )

    op.create_table(
        "connector_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("connector_key", sa.String(), nullable=False),
        sa.Column("vault_item_id", sa.String(), nullable=True),
        sa.Column("settings", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(["institution_id"], ["institution.id"]),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("connector_config", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_connector_config_institution_id"), ["institution_id"], unique=True
        )
    op.execute(
        "INSERT INTO connector_config "
        "(id, institution_id, connector_key, vault_item_id, settings, enabled) "
        "SELECT id, institution_id, connector_key, vault_item_id, settings, enabled FROM provider"
    )

    with op.batch_alter_table(
        "sync_run", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_column("provider_key")

    with op.batch_alter_table("provider_account", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_provider_account_account_id"))
        batch_op.drop_index(batch_op.f("ix_provider_account_provider_id"))
    op.drop_table("provider_account")
    with op.batch_alter_table("provider", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_provider_institution_id"))
    op.drop_table("provider")
