"""add contacts and account nicknames, and turn the saved names into the owner

A contact is a person or business on the other side of your transactions, or you. This revision
creates the tables for contacts (contact, contact_name, contact_pattern, contact_account) and for
account nicknames, and replaces the self_names preference with the owner contact.

The saved names become the owner, who is at most one contact:

- The longest saved name of two or more words is the owner's structured name: its first word is
  the given name, its last word the family name, and the words between are the middle name.
- Every other saved name that none of that name's forms covers is kept whole as the given name of
  another name of the owner, so each saved name is still recognized as it was. The forms are the
  ones in omsync.ledger.contacts, written out here so this revision keeps its meaning: lowercase,
  single-spaced, periods dropped.
- With saved names but none of two or more words, every name is kept whole, and the owner is
  displayed by the longest.
- With no saved names no owner is created. The preference is deleted either way.

The counterparty table is untouched.

The downgrade writes the owner's names back as the self_names preference, each as "given
[middle] family", followed by the account nicknames, which the older code knew only as saved names.
Contacts other than the owner, with their patterns and accounts, are lost, and so are the forms
the owner's names are recognized in beyond the names themselves. It then drops the tables.

Revision ID: c1f7a9d3e582
Revises: b3e8f1a6c429
Create Date: 2026-10-24 22:00:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f7a9d3e582"
down_revision: str | None = "b3e8f1a6c429"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SELF_NAMES = "self_names"


def upgrade() -> None:
    op.create_table(
        "contact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "person",
                "business",
                name="contact_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "co_owner",
                "other",
                name="contact_role",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="other",
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "uq_contact_one_owner",
        "contact",
        ["role"],
        unique=True,
        sqlite_where=sa.text("role = 'owner'"),
    )
    op.create_table(
        "contact_name",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("given", sa.String(), nullable=False),
        sa.Column("middle", sa.String(), nullable=True),
        sa.Column("family", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["contact_id"], ["contact.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contact_id", "position", name="uq_contact_name_contact_position"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_contact_name_contact_id", "contact_name", ["contact_id"])
    op.create_table(
        "contact_pattern",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
        sa.CheckConstraint("side IN ('out', 'in')", name="ck_contact_pattern_side"),
        sa.ForeignKeyConstraint(["contact_id"], ["contact.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "contact_id", "side", "pattern", name="uq_contact_pattern_side_pattern"
        ),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_contact_pattern_contact_id", "contact_pattern", ["contact_id"])
    op.create_table(
        "contact_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("last4", sa.String(), nullable=True),
        sa.CheckConstraint(
            "last4 IS NULL OR (length(last4) = 4 AND last4 NOT GLOB '*[^0-9]*')",
            name="ck_contact_account_last4",
        ),
        sa.ForeignKeyConstraint(["contact_id"], ["contact.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_contact_account_contact_id", "contact_account", ["contact_id"])
    op.create_table(
        "account_nickname",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("nickname", sa.String(collation="NOCASE"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "nickname", name="uq_account_nickname_account_nickname"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_account_nickname_account_id", "account_nickname", ["account_id"])
    _make_owner(_saved_names())


def downgrade() -> None:
    bind = op.get_bind()
    names = [
        " ".join(part for part in row if part)
        for row in bind.execute(
            sa.text(
                "SELECT n.given, n.middle, n.family FROM contact_name n "
                "JOIN contact c ON c.id = n.contact_id WHERE c.role = 'owner' "
                "ORDER BY n.position, n.id"
            )
        )
    ]
    names.extend(
        bind.execute(
            sa.text("SELECT nickname FROM account_nickname ORDER BY account_id, id")
        ).scalars()
    )
    if names:
        bind.execute(
            sa.text("INSERT OR REPLACE INTO preference (key, value) VALUES (:key, :value)"),
            {"key": SELF_NAMES, "value": json.dumps(names)},
        )
    op.drop_table("account_nickname")
    op.drop_table("contact_account")
    op.drop_table("contact_pattern")
    op.drop_table("contact_name")
    op.drop_table("contact")


def _saved_names() -> list[str]:
    """The saved names, cleaned as the preference cleaned them, and the preference is deleted."""
    bind = op.get_bind()
    raw = bind.execute(
        sa.text("SELECT value FROM preference WHERE key = :key"), {"key": SELF_NAMES}
    ).scalar_one_or_none()
    bind.execute(sa.text("DELETE FROM preference WHERE key = :key"), {"key": SELF_NAMES})
    value = json.loads(raw) if raw is not None else []
    saved = [str(name) for name in value] if isinstance(value, list) else []
    names: list[str] = []
    for name in saved:
        clean = " ".join(name.split())
        if clean and clean.casefold() not in {other.casefold() for other in names}:
            names.append(clean)
    return names


def _make_owner(names: list[str]) -> None:
    if not names:
        return
    bind = op.get_bind()
    multiword = [
        name
        for name in names
        if len(name.split()) >= 2 and _plain(name.split()[0]) and _plain(name.split()[-1])
    ]
    structured = max(multiword, key=len) if multiword else None
    rows: list[tuple[str, str | None, str | None]] = []
    if structured is not None:
        words = structured.split()
        given, family = words[0], words[-1]
        middle = " ".join(words[1:-1]) or None
        rows.append((given, middle, family))
        forms = _forms(given, middle, family)
        rows.extend((name, None, None) for name in names if _plain(name) not in forms)
        display = structured
    else:
        rows.extend((name, None, None) for name in names)
        display = max(names, key=len)
    bind.execute(
        sa.text("INSERT INTO contact (kind, role, name) VALUES ('person', 'owner', :name)"),
        {"name": display},
    )
    owner_id = bind.execute(sa.text("SELECT id FROM contact WHERE role = 'owner'")).scalar_one()
    for position, (given, middle, family) in enumerate(rows):
        bind.execute(
            sa.text(
                "INSERT INTO contact_name (contact_id, position, given, middle, family) "
                "VALUES (:contact_id, :position, :given, :middle, :family)"
            ),
            {
                "contact_id": owner_id,
                "position": position,
                "given": given,
                "middle": middle,
                "family": family,
            },
        )


def _plain(text: str) -> str:
    return " ".join(text.lower().replace(".", "").split())


def _forms(given: str, middle: str | None, family: str) -> set[str]:
    g, m, f = _plain(given), _plain(middle or ""), _plain(family)
    forms = {f"{g} {f}", f"{f} {g}", f"{g[0]} {f}", f"{f} {g[0]}", f"{g} {f[0]}"}
    if m:
        forms |= {f"{g} {m} {f}", f"{g} {m[0]} {f}", f"{f} {g} {m}", f"{f} {g} {m[0]}"}
    return forms
