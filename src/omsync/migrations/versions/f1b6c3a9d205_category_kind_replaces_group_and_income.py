"""a category's kind replaces its group, income flag, and flex type

A category had three fields that together said what it was for: a group, an income flag, and a flex
type. The group did two jobs. It sectioned the budget page, which flex budgeting made unnecessary,
and a group named Ignore left a category's money out of reports. One kind now says all of it:
fixed, flexible, non-monthly, unassigned, income, or excluded.

An Ignore group becomes excluded. Otherwise an income flag becomes income, and any other category
keeps its flex type, with none becoming unassigned. The group's other names are not kept.

The downgrade rebuilds the three fields. It cannot recover the old group names: excluded goes back
to the group Ignore, income to Income, and the rest to Ungrouped.

Revision ID: f1b6c3a9d205
Revises: e7a0f8c2d4b1
Create Date: 2026-10-17 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1b6c3a9d205"
down_revision: str | None = "e7a0f8c2d4b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("fixed", "flexible", "non_monthly", "unassigned", "income", "excluded")
FLEX_TYPES = ("fixed", "flexible", "non_monthly", "none")


def upgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    op.add_column(
        "category",
        sa.Column("kind", sa.String(11), server_default="unassigned", nullable=False),
    )
    bind.exec_driver_sql(
        "UPDATE category SET kind = CASE"
        " WHEN lower(\"group\") = 'ignore' THEN 'excluded'"
        " WHEN is_income THEN 'income'"
        " WHEN flex_type = 'none' THEN 'unassigned'"
        " ELSE flex_type END"
    )
    with op.batch_alter_table(
        "category", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint("flex_type", type_="check")
        batch_op.drop_column("group")
        batch_op.drop_column("flex_type")
        batch_op.drop_column("is_income")
        batch_op.create_check_constraint("category_kind", _in("kind", KINDS))
    _check_foreign_keys(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    op.add_column("category", sa.Column("group", sa.String(), server_default="", nullable=False))
    op.add_column(
        "category",
        sa.Column("flex_type", sa.String(11), server_default="none", nullable=False),
    )
    op.add_column(
        "category",
        sa.Column("is_income", sa.Boolean(), server_default=sa.text("0"), nullable=False),
    )
    bind.exec_driver_sql(
        "UPDATE category SET"
        " \"group\" = CASE kind WHEN 'excluded' THEN 'Ignore' WHEN 'income' THEN 'Income'"
        " ELSE 'Ungrouped' END,"
        " is_income = (kind = 'income'),"
        " flex_type = CASE WHEN kind IN ('fixed', 'flexible', 'non_monthly') THEN kind"
        " ELSE 'none' END"
    )
    with op.batch_alter_table(
        "category", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint("category_kind", type_="check")
        batch_op.drop_column("kind")
        batch_op.create_check_constraint("flex_type", _in("flex_type", FLEX_TYPES))
    _check_foreign_keys(bind)


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
