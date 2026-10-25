"""put a space where Chase cut NAME between two words

Chase cuts NAME at 32 characters and continues the description in MEMO. Revision d9b4e6a1c372
joined the two with no space whenever NAME was 32 characters, assuming the cut fell inside a word.
Sometimes it falls between words, which glued them: "Online Transfer 12345678901 fromJoint
Account 1111". Import now adds the space when NAME ends in "from" or "to", or MEMO starts with a
transaction# or a Zelle confirmation. This revision does the same for transactions already
imported.

A transaction is changed through the statement row that created it, as in d9b4e6a1c372. For Chase
and Chase Business rows, payee_raw becomes the new join only when it still equals the old one, and
payee only when it still equals the old payee_raw, stripped, so a payee you renamed stays. The
statement rows are not touched, and batch history is not rewritten, so undoing an import made
before this revision reports the changed transactions as a conflict.

Both joins are written out here so this revision keeps its meaning. The downgrade restores the
old join on the same terms.

Revision ID: b3e8f1a6c429
Revises: d55c543c47da
Create Date: 2026-10-24 21:00:00.000000

"""

import re
from collections.abc import Callable, Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e8f1a6c429"
down_revision: str | None = "d55c543c47da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHASE_INSTITUTIONS = ("chase", "chase_business")
NAME_CUT_LENGTH = 32
_NAME_ENDS_WORD = re.compile(r"\s(?:from|to)$")
_MEMO_STARTS_WORD = re.compile(r"transaction#|(?:BAC|JPM)[a-z0-9]{7,}")


def upgrade() -> None:
    _rejoin(_old_join, _new_join)


def downgrade() -> None:
    _rejoin(_new_join, _old_join)


def _rejoin(
    before: Callable[[str, str | None], str], after: Callable[[str, str | None], str]
) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT t.id, t.payee, t.payee_raw, r.payee_raw AS name, r.memo_raw AS note "
            'FROM "transaction" t '
            "JOIN transaction_row tr ON tr.transaction_id = t.id AND tr.match_kind = 'created' "
            "JOIN statement_row r ON r.id = tr.statement_row_id "
            "JOIN statement_file f ON f.id = r.statement_file_id "
            "JOIN account a ON a.id = f.account_id "
            "JOIN institution i ON i.id = a.institution_id "
            "WHERE i.key IN ('chase', 'chase_business') AND f.format_key = 'ofx'"
        )
    ).all()
    for txn_id, payee, payee_raw, name, note in rows:
        old, new = before(name, note), after(name, note)
        if old == new or payee_raw != old:
            continue
        values: dict[str, object] = {"payee_raw": new}
        if payee == old.strip():
            values["payee"] = new.strip()
        assignments = ", ".join(f"{column} = :{column}" for column in values)
        bind.execute(
            sa.text(f'UPDATE "transaction" SET {assignments} WHERE id = :id'),
            {**values, "id": txn_id},
        )


def _old_join(name: str, memo: str | None) -> str:
    text = name.strip()
    note = (memo or "").strip()
    if not note or note.lower() in text.lower():
        return text
    separator = "" if len(name) == NAME_CUT_LENGTH else " "
    return f"{text}{separator}{note}"


def _new_join(name: str, memo: str | None) -> str:
    text = name.strip()
    note = (memo or "").strip()
    if not note or note.lower() in text.lower():
        return text
    cut_inside_word = (
        len(name) == NAME_CUT_LENGTH
        and not _NAME_ENDS_WORD.search(name)
        and not _MEMO_STARTS_WORD.match(note)
    )
    return f"{text}{'' if cut_inside_word else ' '}{note}"
