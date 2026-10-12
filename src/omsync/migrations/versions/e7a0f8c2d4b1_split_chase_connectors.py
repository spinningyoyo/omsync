"""split Chase personal and business connectors

Chase personal and Chase Business have distinct authentication interfaces and request
requirements. Providers at the chase_business institution that used the shared chase connector
move to the explicit chase_business connector.

Revision ID: e7a0f8c2d4b1
Revises: dc76ac8ff6c6
Create Date: 2026-10-16 22:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a0f8c2d4b1"
down_revision: str | None = "dc76ac8ff6c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE provider SET connector_key = 'chase_business' "
        "WHERE connector_key = 'chase' AND institution_id IN "
        "(SELECT id FROM institution WHERE key = 'chase_business')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE provider SET connector_key = 'chase' "
        "WHERE connector_key = 'chase_business' AND institution_id IN "
        "(SELECT id FROM institution WHERE key = 'chase_business')"
    )
