"""drop keyword.priority and category_rule.from_category_id where they are left over

Revision b3f7c2e9a051 was edited after a database had run an earlier version of it, which kept
keyword.priority and category_rule.from_category_id, with the checks that name that column. Both
are unused: every priority is 0 and every from_category_id is empty, since rules no longer read a
keyword's category. This drops them where they remain and does nothing on a database that ran the
final b3f7c2e9a051. No keyword, rule, or transaction changes.

The downgrade does nothing: b3f7c2e9a051's own downgrade brings the columns back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d1e4a2b9f0"
down_revision: str | None = "b3f7c2e9a051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTOINCREMENT = {"sqlite_autoincrement": True}
HAS_CONDITION = (
    "paid_by IS NOT NULL OR below_cents IS NOT NULL"
    " OR at_least_cents IS NOT NULL OR other_side IS NOT NULL OR account_id IS NOT NULL"
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "priority" in {column["name"] for column in inspector.get_columns("keyword")}:
        with op.batch_alter_table(
            "keyword", recreate="always", table_kwargs=_AUTOINCREMENT
        ) as batch:
            batch.drop_column("priority")
    if "from_category_id" in {column["name"] for column in inspector.get_columns("category_rule")}:
        bind.execute(sa.text("DELETE FROM category_rule WHERE from_category_id IS NOT NULL"))
        checks = {check["name"] for check in inspector.get_check_constraints("category_rule")}
        with op.batch_alter_table(
            "category_rule", recreate="always", table_kwargs=_AUTOINCREMENT
        ) as batch:
            for name in (
                "ck_category_rule_paid_by",
                "ck_category_rule_categories_differ",
                "ck_category_rule_has_condition",
            ):
                if name in checks:
                    batch.drop_constraint(name, type_="check")
            batch.drop_column("from_category_id")
            batch.create_check_constraint("ck_category_rule_has_condition", HAS_CONDITION)


def downgrade() -> None:
    pass
