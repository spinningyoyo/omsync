"""require positive link amounts

Receipt links were stored with the signed charge, so a purchase's receipt link was negative.
Coverage sums link magnitudes, so the purchase looked twice as unexplained as it was. Receipt
links now hold the magnitude, and this revision corrects the rows already stored: it flips the
sign of each negative receipt link, then adds a CHECK constraint that refuses any link amount
that is not positive.

The rebuild keeps AUTOINCREMENT, which a batch rebuild would otherwise drop (see a29c13f5b31b).

The downgrade drops the constraint. It does not restore the signs: they were the bug.

Revision ID: 5f0c2e7a9b31
Revises: 4cb3bb1205c6
Create Date: 2026-10-12 09:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5f0c2e7a9b31"
down_revision: str | None = "4cb3bb1205c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_link_amount_positive"


def upgrade() -> None:
    op.execute(
        "UPDATE link SET amount_cents = -amount_cents WHERE kind = 'receipt' AND amount_cents < 0"
    )
    with op.batch_alter_table(
        "link", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.create_check_constraint(CONSTRAINT, "amount_cents > 0")


def downgrade() -> None:
    with op.batch_alter_table(
        "link", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint(CONSTRAINT, type_="check")
