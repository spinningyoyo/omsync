"""a category may name a Lucide icon

The icon is the name of a Lucide icon, such as piggy-bank. A category with none shows the default
icon, so every existing category keeps NULL. The downgrade drops the column and the choices with it.

Revision ID: a4d1e8b2c6f3
Revises: f1b6c3a9d205
Create Date: 2026-10-17 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4d1e8b2c6f3"
down_revision: str | None = "f1b6c3a9d205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("category", sa.Column("icon", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("category", "icon")
