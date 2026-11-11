"""name each reconcile rule in words instead of a number or abbreviation

A link and a turned-down proposal remember the rule that made it, in a rule column and in the
"rule" field of their evidence. The evidence also keeps the rule's label as "description". Batch
changes keep copies of both rows so an undo can check them. Every old key becomes its new key, and
a label that is still the old wording becomes the new wording. A label a person could not have
produced is left alone.

The downgrade reverses the mapping. The keys 7b, cp, p2p and p2p_solo are no longer proposed;
they only name older links and rejections, and they come back the same way.

Revision ID: a3e7c5d9b142
Revises: d4a8f2c61b3e
Create Date: 2026-11-10 12:00:00.000000

"""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3e7c5d9b142"
down_revision: str | None = "d4a8f2c61b3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (old key, old label, new key, new label)
_RULES = (
    (
        "1a",
        "Transfer: Conf# Hash Match",
        "transfer_same_confirmation",
        "Transfer with the same confirmation number",
    ),
    (
        "1b",
        "Transfer: Zelle Conf# Match",
        "zelle_same_confirmation",
        "Zelle with the same confirmation number",
    ),
    (
        "1c",
        "Transfer: Shared Transaction ID",
        "transfer_same_transaction_id",
        "Transfer with the same transaction ID",
    ),
    (
        "1d",
        "Transfer: Account Reference",
        "transfer_names_account",
        "Transfer whose description names the other account",
    ),
    ("2", "Transfer: CC Bill Payment", "card_bill_payment", "Credit card bill payment"),
    ("2a", "Joint: Card Bill Settlement", "joint_card_bill", "Joint card bill settled"),
    (
        "2b",
        "Transfer: CC Credit Balance Refund",
        "card_credit_refund",
        "Credit card credit balance refunded",
    ),
    (
        "3",
        "Transfer: Generic (No Conf#)",
        "transfer_same_amount",
        "Transfer with the same amount and date, no confirmation",
    ),
    ("rv", "Reversal: Same-Account", "reversal_same_account", "Reversal on the same account"),
    ("7a", "Merchant: Same-Account Return", "return_same_account", "Return on the same account"),
    (
        "7c",
        "Merchant: Same-Account PayPal Return (±$0.01)",
        "paypal_return_same_account",
        "PayPal return on the same account (within $0.01)",
    ),
    (
        "7d",
        "Merchant: Cross-Account Return (Brand Match)",
        "return_other_account_same_store",
        "Return at the same store on another account",
    ),
    ("rt", "P2P: Round Trip", "p2p_round_trip", "Money sent and sent back"),
    (
        "ru",
        "Return: Round Trip via Untracked Account",
        "return_via_untracked_account",
        "Return through an untracked account",
    ),
    ("7b", "Merchant: Cross-Account Return", "return_other_account", "Return on another account"),
    ("cp", "P2P: Named Counterparty", "p2p_named_counterparty", "Payment app with a named contact"),
    ("p2p", "P2P: General Reimbursement", "p2p_reimbursement", "Payment app reimbursement"),
    (
        "p2p_solo",
        "P2P: Solo Inflow (No Debit Match)",
        "p2p_unmatched_inflow",
        "Payment app money in, no matching payment",
    ),
)

# table -> columns that hold an evidence object
_EVIDENCE = (("link", "evidence"), ("rejected_proposal", "evidence"))


def _maps(forward: bool) -> tuple[dict[str, str], dict[str, str]]:
    keys = {(r[0] if forward else r[2]): (r[2] if forward else r[0]) for r in _RULES}
    labels = {(r[1] if forward else r[3]): (r[3] if forward else r[1]) for r in _RULES}
    return keys, labels


def _evidence(value: Any, keys: Mapping[str, str], labels: Mapping[str, str]) -> Any:
    """Return the evidence with its rule and label renamed, or None if nothing changes."""
    if not isinstance(value, dict):
        return None
    out = dict(value)
    if out.get("rule") in keys:
        out["rule"] = keys[out["rule"]]
    if out.get("description") in labels:
        out["description"] = labels[out["description"]]
    return out if out != value else None


def _rewrite_json(table: str, column: str, change: Callable[[Any], Any]) -> None:
    """Rewrite one JSON column in place, where change returns a new value or None for no change."""
    bind = op.get_bind()
    rows = bind.execute(sa.text(f'SELECT id, "{column}" FROM "{table}"')).all()
    for row_id, raw in rows:
        if raw is None:
            continue
        new = change(json.loads(raw))
        if new is not None:
            bind.execute(
                sa.text(f'UPDATE "{table}" SET "{column}" = :v WHERE id = :id'),
                {"v": json.dumps(new), "id": row_id},
            )


def _row_snapshot(value: Any, keys: Mapping[str, str], labels: Mapping[str, str]) -> Any:
    """A batch change keeps a whole row: its rule column and evidence object."""
    if not isinstance(value, dict):
        return None
    out = dict(value)
    if out.get("rule") in keys:
        out["rule"] = keys[out["rule"]]
    changed = _evidence(out.get("evidence"), keys, labels)
    if changed is not None:
        out["evidence"] = changed
    return out if out != value else None


def _rename(forward: bool) -> None:
    keys, labels = _maps(forward)
    bind = op.get_bind()
    for old, new in keys.items():
        bind.execute(
            sa.text("UPDATE rejected_proposal SET rule = :new WHERE rule = :old"),
            {"new": new, "old": old},
        )
    for table, column in _EVIDENCE:
        _rewrite_json(table, column, lambda v: _evidence(v, keys, labels))
    # batch_change.id is the key of its table, and table_name picks the rows that hold links.
    for column in ("before", "after"):
        rows = bind.execute(
            sa.text(
                f'SELECT id, "{column}" FROM batch_change'
                " WHERE table_name IN ('link', 'rejected_proposal')"
            )
        ).all()
        for row_id, raw in rows:
            if raw is None:
                continue
            new = _row_snapshot(json.loads(raw), keys, labels)
            if new is not None:
                bind.execute(
                    sa.text(f'UPDATE batch_change SET "{column}" = :v WHERE id = :id'),
                    {"v": json.dumps(new), "id": row_id},
                )


def upgrade() -> None:
    _rename(forward=True)


def downgrade() -> None:
    _rename(forward=False)
