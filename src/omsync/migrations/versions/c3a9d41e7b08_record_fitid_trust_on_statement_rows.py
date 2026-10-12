"""record fitid trust on statement rows

Import used to fingerprint-join two rows whose trusted FITIDs differed, which silently merged
separate purchases from partial files. To refuse that, a row must say whether its format vouched
for its FITID when it was imported. A stored FITID does not say so: untrusted formats store theirs
too.

Rows imported before this revision have no record, so they are stored as untrusted. A
FITID-conflict check therefore cannot see them. Rows are immutable after insert, so nothing else
can set the flag later.

Revision ID: c3a9d41e7b08
Revises: 5f0c2e7a9b31
Create Date: 2026-10-12 09:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a9d41e7b08"
down_revision: str | None = "5f0c2e7a9b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "statement_row",
        sa.Column("fitid_trusted", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    with op.batch_alter_table(
        "statement_row", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_column("fitid_trusted")
