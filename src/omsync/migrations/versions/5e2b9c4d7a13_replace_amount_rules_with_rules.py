"""replace amount rules with rules

An amount rule was one shape of rule: a payee keyword's category, a limit, and a person or any. A
rule is an ordered list of "if all these conditions hold, set this category": the keyword's category
and list, an amount from below and at least, who is on the other side (a person, one contact, or one
of your own accounts), and the account. The first rule that holds, in position order, sets the
category, with origin rule. Keywords still do all the matching of description text.

Every amount_rule becomes a rule with the same id, the same conditions, and its place in the order
that categorize used before: by limit, then id. A rule with the person scope gets other side
person, and the others get none. The amount_rule table is dropped.

The transaction and split tables allow origin rule, so both are rebuilt with foreign keys off, as
a29c13f5b31b does, keeping AUTOINCREMENT. No origin changes now: a transaction set by an amount rule
keeps origin keyword until categorize runs again, when it becomes rule. Batches made before this
revision stay in the history, and undoing one is refused, because the amount_rule table is gone.

The downgrade recreates amount_rule and copies back the rules it can express: a unique or generic
keyword list with a from category, an under limit and no minimum, with other side person or none,
and no contact or account. Those keep their ids and come back with their scope. Any other rule is
dropped, and the order is lost, because the previous revision applies the lowest limit first. Origin
rule becomes keyword, so the next categorize settles them. A batch that set a category to rule
stays in the history, and undoing it after a downgrade is refused, because its rows now hold
keyword rather than the origin that batch left.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e2b9c4d7a13"
down_revision: str | None = "421166fb58e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OTHER_SIDES = ("person", "contact", "owner_account")
ORIGINS = ("none", "keyword", "rule", "receipt", "manual")
PREVIOUS_ORIGINS = ("none", "keyword", "receipt", "manual")


def upgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    _create_category_rule()
    rows = bind.execute(
        sa.text(
            "SELECT id, list_key, from_category_id, below_cents, to_category_id, enabled, scope "
            "FROM amount_rule ORDER BY below_cents, id"
        )
    ).all()
    for position, (rule_id, list_key, from_id, below, to_id, enabled, scope) in enumerate(
        rows, start=1
    ):
        bind.execute(
            sa.text(
                "INSERT INTO category_rule (id, position, enabled, from_category_id, list_key, "
                "below_cents, to_category_id, other_side) VALUES (:id, :position, :enabled, "
                ":from_id, :list_key, :below, :to_id, :other)"
            ),
            {
                "id": rule_id,
                "position": position,
                "enabled": enabled,
                "from_id": from_id,
                "list_key": list_key,
                "below": below,
                "to_id": to_id,
                "other": "person" if scope == "person" else None,
            },
        )
    op.drop_table("amount_rule")
    _replace_origin_check(ORIGINS)
    _check_foreign_keys(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    bind.exec_driver_sql(
        "UPDATE \"transaction\" SET category_origin = 'keyword' WHERE category_origin = 'rule'"
    )
    bind.exec_driver_sql(
        "UPDATE split SET category_origin = 'keyword' WHERE category_origin = 'rule'"
    )
    _create_amount_rule()
    rows = bind.execute(
        sa.text(
            "SELECT id, list_key, from_category_id, below_cents, to_category_id, enabled, "
            "other_side FROM category_rule WHERE list_key IS NOT NULL"
            " AND from_category_id IS NOT NULL "
            "AND below_cents IS NOT NULL AND at_least_cents IS NULL AND account_id IS NULL "
            "AND contact_id IS NULL AND (other_side IS NULL OR other_side = 'person') "
            "ORDER BY position, id"
        )
    ).all()
    for rule_id, list_key, from_id, below, to_id, enabled, other in rows:
        bind.execute(
            sa.text(
                "INSERT INTO amount_rule (id, list_key, from_category_id, below_cents, "
                "to_category_id, enabled, scope) VALUES (:id, :list_key, :from_id, :below, "
                ":to_id, :enabled, :scope)"
            ),
            {
                "id": rule_id,
                "list_key": list_key,
                "from_id": from_id,
                "below": below,
                "to_id": to_id,
                "enabled": enabled,
                "scope": "person" if other == "person" else "any",
            },
        )
    op.drop_table("category_rule")
    _replace_origin_check(PREVIOUS_ORIGINS)
    _check_foreign_keys(bind)


def _create_category_rule() -> None:
    op.create_table(
        "category_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("from_category_id", sa.Integer(), nullable=True),
        sa.Column("list_key", sa.String(), nullable=True),
        sa.Column("below_cents", sa.Integer(), nullable=True),
        sa.Column("at_least_cents", sa.Integer(), nullable=True),
        sa.Column(
            "other_side",
            sa.Enum(
                *OTHER_SIDES,
                name="rule_other_side",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=True,
        ),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("to_category_id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "from_category_id IS NOT NULL OR below_cents IS NOT NULL"
            " OR at_least_cents IS NOT NULL OR other_side IS NOT NULL OR account_id IS NOT NULL",
            name="ck_category_rule_has_condition",
        ),
        sa.CheckConstraint(
            "list_key IS NULL OR (from_category_id IS NOT NULL"
            " AND list_key IN ('unique', 'generic'))",
            name="ck_category_rule_list",
        ),
        sa.CheckConstraint("below_cents IS NULL OR below_cents > 0", name="ck_category_rule_below"),
        sa.CheckConstraint(
            "at_least_cents IS NULL OR at_least_cents > 0", name="ck_category_rule_at_least"
        ),
        sa.CheckConstraint(
            "below_cents IS NULL OR at_least_cents IS NULL OR below_cents > at_least_cents",
            name="ck_category_rule_range",
        ),
        sa.CheckConstraint(
            "CASE WHEN other_side = 'contact' THEN contact_id IS NOT NULL"
            " ELSE contact_id IS NULL END",
            name="ck_category_rule_contact",
        ),
        sa.CheckConstraint(
            "from_category_id IS NULL OR from_category_id != to_category_id",
            name="ck_category_rule_categories_differ",
        ),
        sa.ForeignKeyConstraint(["from_category_id"], ["category.id"]),
        sa.ForeignKeyConstraint(["contact_id"], ["contact.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"]),
        sa.ForeignKeyConstraint(["to_category_id"], ["category.id"]),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )


def _create_amount_rule() -> None:
    """The amount_rule table as the previous revision left it."""
    op.create_table(
        "amount_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("list_key", sa.String(), nullable=False),
        sa.Column("from_category_id", sa.Integer(), nullable=False),
        sa.Column("below_cents", sa.Integer(), nullable=False),
        sa.Column("to_category_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "scope",
            sa.Enum(
                "any",
                "person",
                name="amount_rule_scope",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            server_default="any",
            nullable=False,
        ),
        sa.CheckConstraint("below_cents > 0", name="ck_amount_rule_limit_positive"),
        sa.CheckConstraint(
            "from_category_id != to_category_id", name="ck_amount_rule_categories_differ"
        ),
        sa.ForeignKeyConstraint(["from_category_id"], ["category.id"]),
        sa.ForeignKeyConstraint(["to_category_id"], ["category.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "list_key",
            "scope",
            "from_category_id",
            "below_cents",
            name="uq_amount_rule_list_scope_from_limit",
        ),
        sqlite_autoincrement=True,
    )


def _replace_origin_check(values: tuple[str, ...]) -> None:
    """Rebuild transaction and split with a CHECK on category_origin that allows values. A rebuild
    is the only way to change a constraint's text in SQLite."""
    for table in ("transaction", "split"):
        with op.batch_alter_table(
            table, recreate="always", table_kwargs={"sqlite_autoincrement": True}
        ) as batch_op:
            batch_op.drop_constraint("category_origin", type_="check")
            batch_op.create_check_constraint("category_origin", _in("category_origin", values))


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
