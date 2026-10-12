"""give Chase transactions the whole description from the statement

Chase cuts NAME at 32 characters and continues the description in MEMO. Import used to keep only
NAME as the payee and copy MEMO into the transaction memo, which is yours. Import now makes the
payee and raw payee the whole description and leaves the memo empty. No import copies a statement
MEMO into the memo any more. This revision does the same for transactions already imported: Chase
and Chase Business rows get the whole description, and every row's copied MEMO is cleared.

A transaction is changed through the statement row that created it, found by following the
statement file to its account and the account to its institution. For each one:

- For Chase and Chase Business, payee_raw becomes the joined description when it still equals the
  row's NAME.
- payee becomes the joined description only when it still equals the old payee_raw, stripped, so a
  payee you renamed stays.
- memo is cleared only when it still equals the row's MEMO exactly, so a memo you wrote stays.

The statement rows are not touched. Batch history is not rewritten either, so undoing an import
made before this revision reports the changed transactions as a conflict.

The join is the one in omsync.ingest.formats.base, written out here so this revision keeps its
meaning. The downgrade does nothing: the old values cannot be told apart from the new ones.

Revision ID: d9b4e6a1c372
Revises: c5f8a2d7e913
Create Date: 2026-10-22 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9b4e6a1c372"
down_revision: str | None = "c5f8a2d7e913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHASE_INSTITUTIONS = ("chase", "chase_business")
NAME_CUT_LENGTH = 32


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT t.id, t.payee, t.payee_raw, t.memo, r.payee_raw AS name, r.memo_raw AS note, "
            "i.key AS institution_key, f.format_key "
            'FROM "transaction" t '
            "JOIN transaction_row tr ON tr.transaction_id = t.id AND tr.match_kind = 'created' "
            "JOIN statement_row r ON r.id = tr.statement_row_id "
            "JOIN statement_file f ON f.id = r.statement_file_id "
            "JOIN account a ON a.id = f.account_id "
            "LEFT JOIN institution i ON i.id = a.institution_id"
        )
    ).all()
    for txn_id, payee, payee_raw, memo, name, note, institution_key, format_key in rows:
        values: dict[str, object] = {}
        joins = institution_key in CHASE_INSTITUTIONS and format_key == "ofx"
        description = _join(name, note) if joins and payee_raw == name else payee_raw
        if description != payee_raw:
            values["payee_raw"] = description
            if payee == payee_raw.strip():
                values["payee"] = description.strip()
        if note and memo == note:
            values["memo"] = None
        if values:
            assignments = ", ".join(f"{column} = :{column}" for column in values)
            bind.execute(
                sa.text(f'UPDATE "transaction" SET {assignments} WHERE id = :id'),
                {**values, "id": txn_id},
            )


def downgrade() -> None:
    pass


def _join(name: str, memo: str | None) -> str:
    text = name.strip()
    note = (memo or "").strip()
    if not note or note.lower() in text.lower():
        return text
    separator = "" if len(name) == NAME_CUT_LENGTH else " "
    return f"{text}{separator}{note}"
