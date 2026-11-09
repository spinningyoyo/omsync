"""one payee keyword list, and rules that read how money moved

The Unique and Generic keyword lists become one list, payee. Generic held two kinds of keyword:
names of who was paid (stores, places), and words that say how money moved (zelle, venmo,
transfer, check, cash, paypal, sq *, tst*, bank names, the ACH line). The first kind moves into
payee. The second kind is now read from the bank's wording by omsync.ledger.payment_methods, so
those keywords are deleted, and rules say what each method means instead. Rules now categorize
only what no payee keyword matched, so a keyword always beats a rule, and a rule no longer reads
what keywords say:

- A rule that read a Generic keyword of category C now reads "paid by" the methods that C's
  method words named. A rule that read a keyword category any other way, Unique or a Generic
  category with no method words, adjusted a keyword's result, which no rule can do now, and it
  is deleted. The user's data has none.
- Each category that method words assigned gets a rule at the end of the order: paid by those
  methods, set that category. So a payment that only a method word recognized keeps its category,
  now with origin rule once categorize runs again.
- A Generic keyword whose pattern, ignoring case, is also a Unique pattern never applied, since
  Unique won every payee both matched, and it is deleted.

Generic's own zelle keyword had priority, so a store or place word in a Zelle payment's purpose
("Zelle payment to ... for Amazon Books") never won. In one list it would, so the words that
match anywhere in a label change too, as measured on the user's data before this revision:
dinner, food, gas, lunch, restaurant, and store name nothing that pays, and are deleted; amazon,
ebay, walmart, maxx, and jp$ are anchored where the payee starts or ends. A database without these
exact Generic patterns is not affected.

keyword loses priority. Within one list the longer, more specific pattern wins; priority existed to
overrule that. On the user's data only Generic's zelle used it, which this revision deletes, so
dropping it changes no match there. Elsewhere, a keyword that won by priority now wins only if it
is the longer pattern.

category_rule gains paid_by, the method values separated by spaces, and loses list_key and
from_category_id. It is rebuilt to change its checks. No transaction changes now; categorize
settles them on its next run. Batches that added, edited, or deleted a keyword or rule before this
revision stay in the history, and undoing one is refused, because its rows no longer look as it
left them.

The downgrade moves payee keywords back to Unique, gives every keyword priority 0, drops paid_by,
and brings back an empty from_category_id and list_key. The deleted method words and rules are
not restored, and rules that read paid_by are deleted.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f7c2e9a051"
down_revision: str | None = "ee590cd33e1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rebuilding the table keeps AUTOINCREMENT, so a deleted rule's id is never reused.
_AUTOINCREMENT = {"sqlite_autoincrement": True}

# The Generic patterns, ignoring case, that said how money moved, and the method each names. Frozen
# here: payment_methods may grow, but this revision converts the words that existed when it ran.
METHOD_WORDS = {
    "zelle": "transfer",
    "venmo": "transfer",
    "transfer": "transfer",
    "transf": "transfer",
    "des:.+id:": "transfer",
    "deposit": "transfer",
    "counter credit": "transfer",
    "bkofamerica": "transfer",
    "jpmorgan chase": "transfer",
    "wells fargo": "transfer",
    "money network": "transfer",
    "check": "check",
    "cash": "cash",
    "withdrawal": "cash",
    "withdrwl": "cash",
    "paypal": "paypal",
    "pp\\*": "paypal",
    "sq \\*": "square",
    "tst\\*": "toast",
}

HINT_WORDS = {"dinner", "food", "gas", "lunch", "restaurant", "store"}
ANCHORED = {
    "amazon": "^amazon",
    "ebay": "^(paypal \\*)?ebay",
    "walmart": "^walmart",
    "maxx": "^t\\.?j\\.? ?maxx",
    "jp$": " jp$",
}

HAS_CONDITION = (
    "paid_by IS NOT NULL OR below_cents IS NOT NULL"
    " OR at_least_cents IS NOT NULL OR other_side IS NOT NULL OR account_id IS NOT NULL"
)
PREVIOUS_HAS_CONDITION = (
    "from_category_id IS NOT NULL OR below_cents IS NOT NULL"
    " OR at_least_cents IS NOT NULL OR other_side IS NOT NULL OR account_id IS NOT NULL"
)


def upgrade() -> None:
    bind = op.get_bind()
    methods_by_category = _convert_keywords(bind)
    with op.batch_alter_table("keyword", recreate="always", table_kwargs=_AUTOINCREMENT) as batch:
        batch.drop_column("priority")
    with op.batch_alter_table(
        "category_rule", recreate="always", table_kwargs=_AUTOINCREMENT
    ) as batch:
        batch.add_column(sa.Column("paid_by", sa.String(), nullable=True))
        batch.drop_constraint("ck_category_rule_list", type_="check")
        batch.drop_constraint("ck_category_rule_has_condition", type_="check")
        batch.drop_constraint("ck_category_rule_categories_differ", type_="check")
    _convert_rules(bind, methods_by_category)
    with op.batch_alter_table(
        "category_rule", recreate="always", table_kwargs=_AUTOINCREMENT
    ) as batch:
        batch.drop_column("list_key")
        batch.drop_column("from_category_id")
        batch.create_check_constraint("ck_category_rule_has_condition", HAS_CONDITION)


def _convert_keywords(bind: sa.Connection) -> dict[int, set[str]]:
    """Delete method words, hint words, and dead Generic copies, anchor the loose store words,
    merge the rest into payee, and return the methods each category's method words named."""
    unique = {
        pattern.casefold()
        for (pattern,) in bind.execute(
            sa.text("SELECT pattern FROM keyword WHERE list_key = 'unique'")
        )
    }
    methods: dict[int, set[str]] = {}
    gone: list[int] = []
    for keyword_id, pattern, category_id in bind.execute(
        sa.text("SELECT id, pattern, category_id FROM keyword WHERE list_key = 'generic'")
    ).all():
        folded = pattern.casefold()
        if folded in METHOD_WORDS:
            methods.setdefault(category_id, set()).add(METHOD_WORDS[folded])
            gone.append(keyword_id)
        elif folded in unique or folded in HINT_WORDS:
            gone.append(keyword_id)
        elif folded in ANCHORED and ANCHORED[folded] not in unique:
            bind.execute(
                sa.text("UPDATE keyword SET pattern = :pattern WHERE id = :id"),
                {"pattern": ANCHORED[folded], "id": keyword_id},
            )
    for keyword_id in gone:
        bind.execute(sa.text("DELETE FROM keyword WHERE id = :id"), {"id": keyword_id})
    bind.execute(sa.text("UPDATE keyword SET list_key = 'payee' WHERE list_key = 'unique'"))
    bind.execute(sa.text("UPDATE keyword SET list_key = 'payee' WHERE list_key = 'generic'"))
    return methods


def _convert_rules(bind: sa.Connection, methods_by_category: dict[int, set[str]]) -> None:
    """Turn each rule that read a keyword category into a paid-by rule, or delete it when its
    category had no method words, then add a rule per category the method words set."""
    rows = bind.execute(
        sa.text(
            "SELECT id, list_key, from_category_id FROM category_rule "
            "WHERE from_category_id IS NOT NULL ORDER BY position, id"
        )
    ).all()
    for rule_id, list_key, from_id in rows:
        methods = methods_by_category.get(from_id) if list_key == "generic" else None
        if methods:
            bind.execute(
                sa.text("UPDATE category_rule SET paid_by = :paid_by WHERE id = :id"),
                {"paid_by": _paid_by(methods), "id": rule_id},
            )
        else:
            bind.execute(sa.text("DELETE FROM category_rule WHERE id = :id"), {"id": rule_id})
    position = bind.execute(
        sa.text("SELECT COALESCE(MAX(position), 0) FROM category_rule")
    ).scalar()
    for category_id, methods in sorted(methods_by_category.items(), key=_by_first_method):
        position += 1
        bind.execute(
            sa.text(
                "INSERT INTO category_rule (position, enabled, paid_by, to_category_id) "
                "VALUES (:position, 1, :paid_by, :to_id)"
            ),
            {"position": position, "paid_by": _paid_by(methods), "to_id": category_id},
        )


def _paid_by(methods: set[str]) -> str:
    return " ".join(sorted(methods))


def _by_first_method(item: tuple[int, set[str]]) -> tuple[str, int]:
    """Transfer methods first, then the rest by name, so the order reads the same every time."""
    category_id, methods = item
    return ("" if "transfer" in methods else min(methods), category_id)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM category_rule WHERE paid_by IS NOT NULL"))
    bind.execute(sa.text("UPDATE keyword SET list_key = 'unique' WHERE list_key = 'payee'"))
    with op.batch_alter_table("keyword", recreate="always", table_kwargs=_AUTOINCREMENT) as batch:
        batch.add_column(sa.Column("priority", sa.Integer(), server_default="0", nullable=False))
    with op.batch_alter_table(
        "category_rule", recreate="always", table_kwargs=_AUTOINCREMENT
    ) as batch:
        batch.drop_constraint("ck_category_rule_has_condition", type_="check")
        batch.drop_column("paid_by")
        batch.add_column(sa.Column("from_category_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_category_rule_from_category_id", "category", ["from_category_id"], ["id"]
        )
        batch.add_column(sa.Column("list_key", sa.String(), nullable=True))
        batch.create_check_constraint("ck_category_rule_has_condition", PREVIOUS_HAS_CONDITION)
        batch.create_check_constraint(
            "ck_category_rule_list",
            "list_key IS NULL OR (from_category_id IS NOT NULL"
            " AND list_key IN ('unique', 'generic'))",
        )
        batch.create_check_constraint(
            "ck_category_rule_categories_differ",
            "from_category_id IS NULL OR from_category_id != to_category_id",
        )
