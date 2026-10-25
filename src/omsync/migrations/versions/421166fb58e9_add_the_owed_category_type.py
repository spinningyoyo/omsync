"""add the owed category type

A category may be Owed: money you have classified as owed between you and a contact, such as a loan
or money held for them. Reports leave it out, as they leave out Excluded money, and a contact's
balance counts it. Owed is its own type rather than Excluded because a transfer is also Excluded,
and a transfer is money between your own accounts, which must not move a balance.

The category type column has a CHECK constraint that lists its values, so SQLite rebuilds the
category table with owed added to the list, as c5f8a2d7e913 rebuilt it. No row changes: the user
sets a category's type in the app, and no category becomes Owed by itself.

The downgrade makes every owed category excluded, the type that still leaves it out of reports, and
then rebuilds the table with the previous list. A batch that set a category to owed stays in the
history with that value. Undoing such a batch after a downgrade would write owed back, which the
restored constraint refuses.

Revision ID: 421166fb58e9
Revises: a6d3f9c1e842
Create Date: 2026-10-24 23:56:52.985340

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "421166fb58e9"
down_revision: str | None = "a6d3f9c1e842"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORY_TYPES = (
    "fixed",
    "flexible",
    "non_monthly",
    "unassigned",
    "income",
    "excluded",
    "owed",
)
PREVIOUS_CATEGORY_TYPES = ("fixed", "flexible", "non_monthly", "unassigned", "income", "excluded")


def upgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    _replace_type_check(CATEGORY_TYPES)
    _check_foreign_keys(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    bind.exec_driver_sql("UPDATE category SET type = 'excluded' WHERE type = 'owed'")
    _replace_type_check(PREVIOUS_CATEGORY_TYPES)
    _check_foreign_keys(bind)


def _replace_type_check(values: tuple[str, ...]) -> None:
    """Rebuild the category table with a CHECK constraint on its type column that allows values.
    A rebuild is the only way to change a constraint's text in SQLite."""
    with op.batch_alter_table(
        "category", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint("category_type", type_="check")
        batch_op.create_check_constraint("category_type", _in("type", values))


def _in(column: str, values: tuple[str, ...]) -> str:
    """The CHECK expression SQLAlchemy writes for a non-native enum column."""
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _set_foreign_keys(bind: sa.Connection, *, enabled: bool) -> None:
    """SQLite ignores this pragma inside a transaction, so confirm that it took effect."""
    bind.exec_driver_sql(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")
    current = bind.exec_driver_sql("PRAGMA foreign_keys").scalar()
    if bool(current) != enabled:
        raise RuntimeError("SQLite did not change foreign_keys; it is inside a transaction")


def _check_foreign_keys(bind: sa.Connection) -> None:
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"foreign key violations after rebuilding tables: {violations[:5]}")
