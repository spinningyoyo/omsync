"""store the other side of transactions, and turn counterparties into contacts

A transaction and a split can now record who is on the other side of the money: a contact, one of
the contact's accounts, and an origin that says whether omsync recognized it or you chose it. This
revision adds contact_id, contact_account_id, and contact_origin to both tables, and
contact_account_id to contact_pattern, for a pattern that identifies one of the contact's accounts.
Every existing row has no other side stored and the origin none. Recognition fills it in after the
next import.

Every counterparty becomes a contact, in order, and the counterparty table is dropped:

- The kind is kept and the role is other. A person's name of two or more words is parsed as the
  owner's was: the first word is the given name, the last word the family name, and the words
  between are the middle name. A business, or a name of one word, is the given name alone.
- The out and in payee patterns become contact patterns on the same sides, none tied to an account.
- A contact with the same display name, without regard to case, receives the patterns instead of a
  new contact, and keeps its names.
- no_match_expected has no successor and is dropped. A payment it covered is explained once its
  other side is recognized.

The acknowledgment table is dropped too, but only when it is empty. With acknowledgments the
upgrade stops before it changes anything and says how many there are: remove them first, on the
Reconcile page under Explained, with the version of omsync that made them.

The downgrade recreates both tables empty, then turns each contact that has patterns back into a
counterparty with those patterns and no_match_expected false. The contacts stay, for the previous
revision to handle. It removes the new columns by rebuilding the three tables with foreign keys
off, as a29c13f5b31b does, keeping AUTOINCREMENT. The other side stored on transactions and
splits, and the ties of patterns to accounts, are lost.

Revision ID: e8b3d6a4f127
Revises: c1f7a9d3e582
Create Date: 2026-10-25 09:00:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b3d6a4f127"
down_revision: str | None = "c1f7a9d3e582"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORIGINS = ("none", "recognized", "manual")
SIDES = ("out", "in")


def upgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(sa.text("SELECT COUNT(*) FROM acknowledgment")).scalar_one()
    if held:
        noun = "acknowledgment" if held == 1 else "acknowledgments"
        raise RuntimeError(
            f"This database has {held} {noun}, which this version of omsync no longer keeps. "
            "Remove them first: on the Reconcile page, open Explained and press Remove on each, "
            "using the version of omsync you have now. Then upgrade again."
        )
    allowed = ", ".join(f"'{origin}'" for origin in ORIGINS)
    for table in ("transaction", "split"):
        op.execute(
            sa.text(f'ALTER TABLE "{table}" ADD COLUMN contact_id INTEGER REFERENCES contact (id)')
        )
        op.execute(
            sa.text(
                f'ALTER TABLE "{table}" ADD COLUMN contact_account_id INTEGER '
                "REFERENCES contact_account (id)"
            )
        )
        op.execute(
            sa.text(
                f"ALTER TABLE \"{table}\" ADD COLUMN contact_origin VARCHAR(10) DEFAULT 'none' "
                f"NOT NULL CONSTRAINT contact_origin CHECK (contact_origin IN ({allowed}))"
            )
        )
        op.create_index(f"ix_{table}_contact_id", table, ["contact_id"])
    op.execute(
        sa.text(
            "ALTER TABLE contact_pattern ADD COLUMN contact_account_id INTEGER "
            "REFERENCES contact_account (id)"
        )
    )
    _contacts_from_counterparties(bind)
    op.drop_table("acknowledgment")
    op.drop_table("counterparty")


def downgrade() -> None:
    bind = op.get_bind()
    _set_foreign_keys(bind, enabled=False)
    op.create_table(
        "counterparty",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "person",
                "business",
                name="counterparty_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("payee_patterns", sa.JSON(), nullable=False),
        sa.Column("no_match_expected", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "acknowledgment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transaction.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id"),
        sqlite_autoincrement=True,
    )
    _counterparties_from_contacts(bind)
    for table in ("transaction", "split"):
        with op.batch_alter_table(
            table, recreate="always", table_kwargs={"sqlite_autoincrement": True}
        ) as batch_op:
            batch_op.drop_index(f"ix_{table}_contact_id")
            batch_op.drop_constraint("contact_origin", type_="check")
            batch_op.drop_column("contact_id")
            batch_op.drop_column("contact_account_id")
            batch_op.drop_column("contact_origin")
    with op.batch_alter_table(
        "contact_pattern", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ) as batch_op:
        batch_op.drop_column("contact_account_id")
    _check_foreign_keys(bind)


def _contacts_from_counterparties(bind: sa.Connection) -> None:
    existing = {
        name.casefold(): contact_id
        for contact_id, name in bind.execute(sa.text("SELECT id, name FROM contact"))
    }
    rows = bind.execute(
        sa.text("SELECT name, kind, payee_patterns FROM counterparty ORDER BY id")
    ).all()
    for name, kind, raw_patterns in rows:
        contact_id = existing.get(name.casefold())
        if contact_id is None:
            contact_id = _insert_contact(bind, name, kind)
            existing[name.casefold()] = contact_id
        patterns = json.loads(raw_patterns) if isinstance(raw_patterns, str) else raw_patterns
        for side in SIDES:
            for pattern in patterns.get(side, []):
                held = bind.execute(
                    sa.text(
                        "SELECT 1 FROM contact_pattern "
                        "WHERE contact_id = :contact AND side = :side AND pattern = :pattern"
                    ),
                    {"contact": contact_id, "side": side, "pattern": pattern},
                ).first()
                if held is None:
                    bind.execute(
                        sa.text(
                            "INSERT INTO contact_pattern (contact_id, side, pattern) "
                            "VALUES (:contact, :side, :pattern)"
                        ),
                        {"contact": contact_id, "side": side, "pattern": pattern},
                    )


def _insert_contact(bind: sa.Connection, name: str, kind: str) -> int:
    contact_id = bind.execute(
        sa.text("INSERT INTO contact (kind, role, name) VALUES (:kind, 'other', :name)"),
        {"kind": kind, "name": name},
    ).lastrowid
    words = name.split()
    given, middle, family = name, None, None
    if kind == "person" and len(words) >= 2:
        given, middle, family = words[0], " ".join(words[1:-1]) or None, words[-1]
    bind.execute(
        sa.text(
            "INSERT INTO contact_name (contact_id, position, given, middle, family) "
            "VALUES (:contact, 0, :given, :middle, :family)"
        ),
        {"contact": contact_id, "given": given, "middle": middle, "family": family},
    )
    return contact_id


def _counterparties_from_contacts(bind: sa.Connection) -> None:
    contacts = bind.execute(
        sa.text(
            "SELECT id, name, kind FROM contact "
            "WHERE id IN (SELECT contact_id FROM contact_pattern) ORDER BY id"
        )
    ).all()
    for contact_id, name, kind in contacts:
        patterns: dict[str, list[str]] = {side: [] for side in SIDES}
        for side, pattern in bind.execute(
            sa.text("SELECT side, pattern FROM contact_pattern WHERE contact_id = :id ORDER BY id"),
            {"id": contact_id},
        ):
            patterns[side].append(pattern)
        bind.execute(
            sa.text(
                "INSERT INTO counterparty (name, kind, payee_patterns, no_match_expected) "
                "VALUES (:name, :kind, :patterns, 0)"
            ),
            {"name": name, "kind": kind, "patterns": json.dumps(patterns)},
        )


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
