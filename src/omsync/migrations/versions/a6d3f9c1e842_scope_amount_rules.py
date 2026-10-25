"""scope amount rules to payments with a person

An amount rule gains a scope: any, as every rule so far, or person, which applies only to a payment
whose other side is a person (a stored contact of the person kind and the other role, or a payment
that names a person no contact is saved for). Every existing rule keeps the scope any. The scope
joins the unique key, so one list may hold an any rule and a person rule for the same category and
limit.

SQLite cannot change a unique constraint in place, so the table is rebuilt, keeping AUTOINCREMENT
so that no deleted id is reused, as a29c13f5b31b does.

The downgrade deletes the person rules, because the previous revision has no way to say who they
apply to and would apply them to every payment, then rebuilds the table without the column and
with the previous unique key. Batches that changed those rules stay in the history, and undoing one
is refused as the rows are gone.

Revision ID: a6d3f9c1e842
Revises: e8b3d6a4f127
Create Date: 2026-10-26 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6d3f9c1e842"
down_revision: str | None = "e8b3d6a4f127"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPES = ("any", "person")


def upgrade() -> None:
    with op.batch_alter_table(
        "amount_rule", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "scope",
                sa.Enum(
                    *SCOPES,
                    name="amount_rule_scope",
                    native_enum=False,
                    create_constraint=True,
                    validate_strings=True,
                ),
                server_default="any",
                nullable=False,
            )
        )
        batch_op.drop_constraint("uq_amount_rule_list_from_limit", type_="unique")
        batch_op.create_unique_constraint(
            "uq_amount_rule_list_scope_from_limit",
            ["list_key", "scope", "from_category_id", "below_cents"],
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM amount_rule WHERE scope = 'person'"))
    with op.batch_alter_table(
        "amount_rule", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_constraint("uq_amount_rule_list_scope_from_limit", type_="unique")
        batch_op.drop_constraint("amount_rule_scope", type_="check")
        batch_op.drop_column("scope")
        batch_op.create_unique_constraint(
            "uq_amount_rule_list_from_limit", ["list_key", "from_category_id", "below_cents"]
        )
