"""remember why a pair was turned down, and drop the unused proposal table

rejected_proposal kept only the pair key. It now also records the rule and kind that proposed or
made the pair, a snapshot of the evidence, and the reason: a proposal you rejected, or a link you
removed. Rows already there keep their key and time, have no rule, kind, or evidence, and count as
rejected.

The proposal table was never written, because proposals are computed on demand and only the
links and rejections they lead to are stored. It is dropped. The earlier revision a29c13f5b31b
still names it, which is right for the schema at that point in the chain.

The rebuild keeps AUTOINCREMENT, which a batch rebuild would otherwise drop (see a29c13f5b31b).

The downgrade removes the new columns and recreates the proposal table, empty.

Revision ID: e4a7c1d93b60
Revises: d9b4e6a1c372
Create Date: 2026-10-22 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4a7c1d93b60"
down_revision: str | None = "d9b4e6a1c372"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINK_KINDS = ("transfer", "receipt", "refund", "p2p", "joint_settlement")
REASONS = ("rejected", "unlinked")


def _link_kind() -> sa.Enum:
    return sa.Enum(*LINK_KINDS, name="link_kind", native_enum=False, create_constraint=True)


def upgrade() -> None:
    with op.batch_alter_table(
        "rejected_proposal", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.add_column(sa.Column("rule", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("kind", _link_kind(), nullable=True))
        batch_op.add_column(sa.Column("evidence", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "reason",
                sa.Enum(
                    *REASONS, name="rejection_reason", native_enum=False, create_constraint=True
                ),
                server_default="rejected",
                nullable=False,
            )
        )
    op.drop_table("proposal")


def downgrade() -> None:
    op.create_table(
        "proposal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("kind", _link_kind(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("from_transaction_id", sa.Integer(), nullable=True),
        sa.Column("to_transaction_id", sa.Integer(), nullable=True),
        sa.Column("receipt_key", sa.String(), nullable=True),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "open",
                "approved",
                "rejected",
                name="proposal_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="open",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["from_transaction_id"], ["transaction.id"]),
        sa.ForeignKeyConstraint(["to_transaction_id"], ["transaction.id"]),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_proposal_from_transaction_id", "proposal", ["from_transaction_id"])
    op.create_index("ix_proposal_run_id", "proposal", ["run_id"])
    op.create_index("ix_proposal_to_transaction_id", "proposal", ["to_transaction_id"])
    with op.batch_alter_table(
        "rejected_proposal", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint("rejection_reason", type_="check")
        batch_op.drop_constraint("link_kind", type_="check")
        batch_op.drop_column("reason")
        batch_op.drop_column("evidence")
        batch_op.drop_column("kind")
        batch_op.drop_column("rule")
