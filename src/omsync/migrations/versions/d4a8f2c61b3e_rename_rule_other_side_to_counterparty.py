"""rename category_rule.other_side to counterparty

The term is now counterparty everywhere. The column and the checks that name it are rebuilt with
the new name. No rule changes.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a8f2c61b3e"
down_revision: str | None = "c7d1e4a2b9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTOINCREMENT = {"sqlite_autoincrement": True}
_VALUES = "('person', 'contact', 'owner_account')"


def _rename(old: str, new: str, enum_check: str, old_enum_check: str) -> None:
    with op.batch_alter_table(
        "category_rule", recreate="always", table_kwargs=_AUTOINCREMENT
    ) as batch:
        for name in ("ck_category_rule_has_condition", "ck_category_rule_contact", old_enum_check):
            batch.drop_constraint(name, type_="check")
        batch.alter_column(old, new_column_name=new)
        batch.create_check_constraint(
            "ck_category_rule_has_condition",
            "paid_by IS NOT NULL OR below_cents IS NOT NULL"
            f" OR at_least_cents IS NOT NULL OR {new} IS NOT NULL OR account_id IS NOT NULL",
        )
        batch.create_check_constraint(
            "ck_category_rule_contact",
            f"CASE WHEN {new} = 'contact' THEN contact_id IS NOT NULL ELSE contact_id IS NULL END",
        )
        batch.create_check_constraint(enum_check, f"{new} IN {_VALUES}")


def upgrade() -> None:
    _rename("other_side", "counterparty", "rule_counterparty", "rule_other_side")


def downgrade() -> None:
    _rename("counterparty", "other_side", "rule_other_side", "rule_counterparty")
