"""rename the kind of an account and of a category to type

An account has a type (checking, savings, credit card, and so on) and so does a category (fixed,
flexible, income, and so on). Both were stored in a column named kind. They are named type now, as
they are in the pages, the commands, and the documentation. Each table's CHECK constraint is
renamed with its column: account_kind becomes account_type and category_kind becomes
category_type.

Two stored copies of the old names are rewritten too. A batch records each row it changed as
column names and values, and undoing a batch writes them back, so the account and category rows in
batch_change use the new column name. The accounts panel's saved grouping, by kind, is now by type.

The downgrade renames everything back.

Revision ID: c5f8a2d7e913
Revises: b7e2d94a1c58
Create Date: 2026-10-21 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5f8a2d7e913"
down_revision: str | None = "b7e2d94a1c58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCOUNT_TYPES = ("checking", "savings", "credit_card", "brokerage", "other")
CATEGORY_TYPES = ("fixed", "flexible", "non_monthly", "unassigned", "income", "excluded")
GROUPING_PREF = "accounts_panel_grouping"


def upgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    _rename(old="kind", new="type")
    _rewrite_batch_changes(bind, old="kind", new="type")
    _rewrite_grouping(bind, old="kind", new="type")
    _check_foreign_keys(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    _rename(old="type", new="kind")
    _rewrite_batch_changes(bind, old="type", new="kind")
    _rewrite_grouping(bind, old="type", new="kind")
    _check_foreign_keys(bind)


def _rename(*, old: str, new: str) -> None:
    """Rename the column and its CHECK constraint on both tables. A rebuild is the only way to
    change a constraint's name or text in SQLite."""
    for table, values in (("account", ACCOUNT_TYPES), ("category", CATEGORY_TYPES)):
        with op.batch_alter_table(
            table, recreate="always", table_kwargs={"sqlite_autoincrement": True}
        ) as batch_op:
            batch_op.drop_constraint(f"{table}_{old}", type_="check")
            batch_op.alter_column(old, new_column_name=new)
            batch_op.create_check_constraint(f"{table}_{new}", _in(new, values))


def _rewrite_batch_changes(bind: sa.Connection, *, old: str, new: str) -> None:
    for column in ("before", "after"):
        bind.exec_driver_sql(
            f"UPDATE batch_change SET {column} = json_set("
            f"json_remove({column}, '$.{old}'), '$.{new}', json_extract({column}, '$.{old}'))"
            f" WHERE table_name IN ('account', 'category')"
            f" AND json_type({column}, '$.{old}') IS NOT NULL"
        )


def _rewrite_grouping(bind: sa.Connection, *, old: str, new: str) -> None:
    bind.exec_driver_sql(
        f"UPDATE preference SET value = '\"{new}\"'"
        f" WHERE key = '{GROUPING_PREF}' AND value = '\"{old}\"'"
    )


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
