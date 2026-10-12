"""acknowledge a transaction instead of rejecting a one-sided proposal

A payment from a person with no debit to pair with used to be proposed as a one-sided P2P link,
and rejecting that proposal remembered the transaction and amount. Those proposals are no longer
made: the transaction is listed as not matched, and you say no counterpart is expected by
acknowledging it. The acknowledgment table holds that decision, and counterparty.no_match_expected
holds the standing form of it.

Every rejection of a one-sided proposal, and every removed one-sided link, becomes an
acknowledgment of its transaction and leaves rejected_proposal. A rejection is told apart by its
key: one transaction ID and an amount, for a transaction that is money in. Rejections of pairs are
untouched. Links already made from one-sided proposals stay.

The downgrade drops the table and the column. Acknowledgments of money-in transactions return as
rejections of one-sided proposals, which is what the older code understands.

Revision ID: f2c8a5d1b906
Revises: e4a7c1d93b60
Create Date: 2026-10-22 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c8a5d1b906"
down_revision: str | None = "e4a7c1d93b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A one-sided key is "<transaction id>|<cents>": digits around one bar, so no comma lists several
# IDs. The transaction is money in for that amount.
ONE_SIDED = """
    "transaction".id = CAST(substr(r.signature, 1, instr(r.signature, '|') - 1) AS INTEGER)
    AND "transaction".amount_cents > 0
    AND "transaction".amount_cents
        = CAST(substr(r.signature, instr(r.signature, '|') + 1) AS INTEGER)
    AND r.signature GLOB '[0-9]*|[0-9]*'
    AND r.signature NOT GLOB '*[^0-9|]*'
    AND length(r.signature) - length(replace(r.signature, '|', '')) = 1
"""


def upgrade() -> None:
    op.create_table(
        "acknowledgment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transaction.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id"),
        sqlite_autoincrement=True,
    )
    op.add_column(
        "counterparty",
        sa.Column("no_match_expected", sa.Boolean(), server_default=sa.text("0"), nullable=False),
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO acknowledgment (transaction_id, note)
            SELECT "transaction".id,
                   CASE r.reason
                       WHEN 'unlinked' THEN 'Carried over from a removed one-sided P2P link'
                       ELSE 'Carried over from a rejected one-sided P2P proposal'
                   END
            FROM rejected_proposal AS r JOIN "transaction" ON {ONE_SIDED}
            GROUP BY "transaction".id
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            DELETE FROM rejected_proposal WHERE id IN (
                SELECT r.id FROM rejected_proposal AS r JOIN "transaction" ON {ONE_SIDED}
            )
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO rejected_proposal (signature, rule, kind, reason)
            SELECT a.transaction_id || '|' || t.amount_cents, 'p2p_solo', 'p2p', 'rejected'
            FROM acknowledgment AS a JOIN "transaction" AS t ON t.id = a.transaction_id
            WHERE t.amount_cents > 0 AND NOT EXISTS (
                SELECT 1 FROM rejected_proposal AS r
                WHERE r.signature = a.transaction_id || '|' || t.amount_cents
            )
            """
        )
    )
    with op.batch_alter_table("counterparty") as batch_op:
        batch_op.drop_column("no_match_expected")
    op.drop_table("acknowledgment")
