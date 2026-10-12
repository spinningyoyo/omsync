"""Your decisions on proposals: approve, reject, link by hand, unlink, and acknowledge.

Each decision is one batch from omsync.core.batches, with an idempotency key, so a repeated call
changes nothing twice. approve re-runs the engine for the range and refuses any signature that is
no longer a current proposal. Partner contributions are never linked: approval only fills in an
empty memo on the partner's transaction. Rejecting a proposal and unlinking a link both remember
the pair, so it is not proposed again. Acknowledging a transaction, or a counterparty, says no
counterpart is expected; it creates no link and covers none of an amount. coverage and
is_reconciled read links; they never store the state.
"""

import re
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    apply,
    make_preview,
    snapshot,
)
from omsync.ledger.counterparties import (
    create_counterparty,
    load_matchers,
    update_counterparty,
    validate_patterns,
)
from omsync.ledger.links import (
    Acknowledgment,
    CounterpartyKind,
    Link,
    LinkKind,
    RejectedProposal,
    RejectionReason,
)
from omsync.ledger.transactions import Transaction
from omsync.reconcile.engine import run
from omsync.reconcile.model import Draft, pair_signature, pair_signature_of, signature
from omsync.reconcile.pool import transaction_description

# Kinds a person may link by hand. Receipt links come from receipt matching.
MAX_NOTE_LENGTH = 200
MANUAL_KINDS = frozenset(
    {LinkKind.transfer, LinkKind.refund, LinkKind.p2p, LinkKind.joint_settlement}
)


class StaleProposal(Exception):
    """A proposal you chose is no longer current: the data changed, or it is not in the range."""

    def __init__(self, message: str, signatures: Sequence[str]) -> None:
        super().__init__(message)
        self.signatures: list[str] = list(signatures)


class NothingChanged(ValueError):
    """Every choice was already recorded. Nothing was written."""


def approve(
    session: Session,
    *,
    start: date,
    end: date,
    signatures: Iterable[str],
    idempotency_key: str,
) -> Batch:
    """Record the chosen proposals as links, in one batch.

    Raises StaleProposal when a signature is not among the current drafts for the range, and
    ValueError when two chosen proposals use the same transaction. Nothing is written in either
    case.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    wanted = list(dict.fromkeys(signatures))
    if not wanted:
        raise ValueError("choose at least one proposal to approve")

    current = {signature(draft): draft for draft in run(session, start=start, end=end).drafts}
    missing = [sig for sig in wanted if sig not in current]
    if missing:
        raise StaleProposal(
            f"{len(missing)} proposal(s) are no longer current; run reconcile again",
            missing,
        )
    return approve_drafts(
        session,
        [current[sig] for sig in wanted],
        idempotency_key=idempotency_key,
        label=f"Approved {len(wanted)} proposal(s)",
    )


def approve_drafts(
    session: Session, drafts: Sequence[Draft], *, idempotency_key: str, label: str
) -> Batch:
    """Record drafts from a run that was just made as links, in one batch labelled `label`.

    The caller took the drafts from engine.run on the current data, so they are current: the
    batch is refused as stale if the data changes before it is applied. Raises ValueError when two
    drafts use the same transaction, and nothing is written.
    """
    _refuse_shared_transactions(drafts)
    changes: list[Change] = [Change("link", None, None, _link_values(d)) for d in drafts]
    changes.extend(_partner_memo_changes(session, drafts))
    preview = make_preview(session, "reconcile.approve", changes, summary={"label": label})
    return apply(session, preview, idempotency_key=idempotency_key)


def reject(
    session: Session,
    *,
    signatures: Iterable[str],
    idempotency_key: str,
    start: date | None = None,
    end: date | None = None,
) -> Batch:
    """Remember these proposals so no proposal for the same transactions and amount returns.

    Rejection is keyed by the pair, not the kind: a pair rejected as a transfer is also
    suppressed as P2P. With a range, the run for it supplies each proposal's rule and evidence to
    keep; a proposal that is no longer current is still remembered, with its kind only. Raises
    NothingChanged when every one was already rejected.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    chosen: dict[str, str] = {}
    for sig in signatures:
        chosen.setdefault(pair_signature_of(sig), sig)
    wanted = list(chosen)
    if not wanted:
        raise ValueError("choose at least one proposal to reject")
    already = set(
        session.scalars(
            select(RejectedProposal.signature).where(RejectedProposal.signature.in_(wanted))
        )
    )
    new = [sig for sig in wanted if sig not in already]
    if not new:
        raise NothingChanged("those proposals were already rejected")
    drafts: dict[str, Draft] = {}
    if start is not None and end is not None:
        drafts = {
            pair_signature_of(signature(d)): d for d in run(session, start=start, end=end).drafts
        }
    changes = [
        Change(
            "rejected_proposal",
            None,
            None,
            _rejection_values(pair, drafts.get(pair), chosen[pair]),
        )
        for pair in new
    ]
    preview = make_preview(
        session,
        "reconcile.reject",
        changes,
        summary={"label": f"Rejected {len(new)} proposal(s)"},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def manual_link(
    session: Session,
    *,
    out_txn_id: int,
    in_txn_id: int,
    kind: LinkKind | str,
    amount_cents: int,
    idempotency_key: str,
) -> Batch:
    """Link a money-out transaction to a money-in transaction by hand, for an amount.

    The amount must be positive and no more than the unlinked part of either side. Raises
    ValueError for a sign or amount problem, and LookupError for a missing transaction.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    link_kind = LinkKind(kind)
    if link_kind not in MANUAL_KINDS:
        raise ValueError(f"a {link_kind.value} link cannot be made by hand")
    if out_txn_id == in_txn_id:
        raise ValueError("a link needs two different transactions")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
        raise ValueError("the amount must be a positive whole number of cents")
    out = _transaction_or_raise(session, out_txn_id)
    inn = _transaction_or_raise(session, in_txn_id)
    if out.amount_cents >= 0:
        raise ValueError("the money-out transaction must be negative")
    if inn.amount_cents <= 0:
        raise ValueError("the money-in transaction must be positive")
    for txn in (out, inn):
        unlinked = abs(txn.amount_cents) - coverage(session, txn.id)
        if amount_cents > unlinked:
            raise ValueError(f"only {unlinked} cents of transaction {txn.id} are unlinked")

    values: dict[str, Any] = {
        "kind": link_kind.value,
        "amount_cents": amount_cents,
        "from_transaction_id": out.id,
        "to_transaction_id": inn.id,
        "receipt_key": None,
        "evidence": {"rule": "manual", "queue": "manual", "confidence": None},
    }
    preview = make_preview(
        session,
        "reconcile.manual_link",
        [Change("link", None, None, values)],
        summary={"label": f"Linked {out.id} to {inn.id} by hand"},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def unlink(session: Session, link_id: int, *, idempotency_key: str) -> Batch:
    """Remove a link, and remember its pair so no proposal for it returns. Raises LookupError if
    the link does not exist.

    The rejection is part of the same batch, so undoing the unlink also forgets it. A receipt link
    is removed without one: receipts are matched by their own application, not proposed here.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = snapshot(session, "link", link_id)
    if before is None:
        raise LookupError(f"no link {link_id}")
    changes = [Change("link", str(link_id), before, None)]
    remembered = _unlink_rejection(session, before)
    if remembered is not None:
        changes.append(Change("rejected_proposal", None, None, remembered))
    preview = make_preview(
        session,
        "reconcile.unlink",
        changes,
        summary={"label": f"Unlinked link {link_id}"},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def acknowledge(
    session: Session, txn_id: int, *, note: str | None = None, idempotency_key: str
) -> Batch:
    """Say that no counterpart is expected for a transaction, with an optional note.

    This is not a link and reconciles nothing. Raises LookupError for a missing transaction,
    NothingChanged when it is already acknowledged, and ValueError for a transaction links already
    cover in full or a note that is too long.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    txn = _transaction_or_raise(session, txn_id)
    clean = " ".join((note or "").split())
    if len(clean) > MAX_NOTE_LENGTH:
        raise ValueError(f"a note can be at most {MAX_NOTE_LENGTH} characters")
    if session.scalar(select(Acknowledgment.id).where(Acknowledgment.transaction_id == txn_id)):
        raise NothingChanged("that transaction is already acknowledged")
    if coverage(session, txn_id) >= abs(txn.amount_cents):
        raise ValueError("links already explain that whole transaction")
    preview = make_preview(
        session,
        "reconcile.acknowledge",
        [Change("acknowledgment", None, None, {"transaction_id": txn_id, "note": clean or None})],
        summary={"label": f"No match expected for transaction {txn_id}"},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def unacknowledge(session: Session, acknowledgment_id: int, *, idempotency_key: str) -> Batch:
    """Remove an acknowledgment, so its transaction is expected to have a counterpart again.
    Raises LookupError if it does not exist."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = snapshot(session, "acknowledgment", acknowledgment_id)
    if before is None:
        raise LookupError(f"no acknowledgment {acknowledgment_id}")
    preview = make_preview(
        session,
        "reconcile.unacknowledge",
        [Change("acknowledgment", str(acknowledgment_id), before, None)],
        summary={"label": f"Expecting a match for transaction {before['transaction_id']} again"},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def acknowledge_counterparty(
    session: Session,
    txn_id: int,
    *,
    idempotency_key: str,
    counterparty_id: int | None = None,
    name: str = "",
    kind: CounterpartyKind | str = CounterpartyKind.business,
    pattern: str = "",
) -> Batch:
    """Say that no counterpart is expected from a transaction's counterparty, now and later.

    With counterparty_id, the saved counterparty is marked. Without it, a new counterparty named
    `name` is saved, with `pattern` on the side the transaction's sign names. Either way the
    counterparty's patterns must match this transaction, so the choice visibly covers it. Raises
    LookupError for a missing transaction or counterparty, ValueError for a refused name or
    pattern, and counterparties.NothingChanged when the counterparty is already marked.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    txn = _transaction_or_raise(session, txn_id)
    text = transaction_description(session, txn)
    if counterparty_id is not None:
        matcher = next((m for m in load_matchers(session) if m.id == counterparty_id), None)
        if matcher is None:
            raise LookupError(f"no counterparty {counterparty_id}")
        if not matcher.matches(text, txn.amount_cents):
            raise ValueError(f"{matcher.name}'s patterns do not match that transaction")
        return update_counterparty(
            session, counterparty_id, no_match_expected=True, idempotency_key=idempotency_key
        )
    side = "out" if txn.amount_cents < 0 else "in"
    patterns = validate_patterns(
        {"out": [pattern] if side == "out" else [], "in": [pattern] if side == "in" else []}
    )
    if not re.search(patterns[side][0], text, re.IGNORECASE):
        raise ValueError("that pattern does not match the transaction")
    return create_counterparty(
        session,
        name=name,
        kind=kind,
        payee_patterns=patterns,
        no_match_expected=True,
        idempotency_key=idempotency_key,
    )


def coverage(session: Session, txn_id: int) -> int:
    """Return the cents that links explain for one transaction, from either side."""
    total = session.scalar(
        select(func.coalesce(func.sum(Link.amount_cents), 0)).where(
            or_(Link.from_transaction_id == txn_id, Link.to_transaction_id == txn_id)
        )
    )
    return int(total or 0)


def is_reconciled(session: Session, txn_id: int) -> bool:
    """True when links explain the transaction's full amount. Raises LookupError if missing."""
    txn = _transaction_or_raise(session, txn_id)
    return coverage(session, txn_id) == abs(txn.amount_cents)


def _transaction_or_raise(session: Session, txn_id: int) -> Transaction:
    txn = session.get(Transaction, txn_id)
    if txn is None:
        raise LookupError(f"no transaction {txn_id}")
    return txn


def _refuse_shared_transactions(selected: Sequence[Draft]) -> None:
    owner: dict[int, str] = {}
    for draft in selected:
        for txn_id in (draft.out_txn_id, draft.in_txn_id):
            if txn_id is None:
                continue
            if txn_id in owner:
                raise ValueError(
                    f"transaction {txn_id} is in two chosen proposals; approve only one of them"
                )
            owner[txn_id] = signature(draft)


def _link_values(draft: Draft) -> dict[str, Any]:
    evidence: dict[str, Any] = dict(draft.evidence)
    evidence["rule"] = draft.rule_key
    evidence["confidence"] = draft.confidence
    evidence["queue"] = draft.queue.value
    evidence["proposal_signature"] = signature(draft)
    return {
        "kind": draft.kind.value,
        "amount_cents": draft.amount_cents,
        "from_transaction_id": draft.out_txn_id,
        "to_transaction_id": draft.in_txn_id,
        "receipt_key": None,
        "evidence": evidence,
    }


def _partner_memo_changes(session: Session, selected: Sequence[Draft]) -> list[Change]:
    """Memo updates for partner contributions, setting only memos that are empty today."""
    changes: list[Change] = []
    seen: set[int] = set()
    for draft in selected:
        for note in draft.evidence.get("partner_contributions", []):
            txn_id = int(note["txn_id"])
            if txn_id in seen:
                continue
            seen.add(txn_id)
            txn = session.get(Transaction, txn_id)
            if txn is None or txn.memo:
                continue
            changes.append(
                Change(
                    "transaction",
                    str(txn_id),
                    {"memo": txn.memo},
                    {"memo": str(note["memo"])},
                )
            )
    return changes


def _rejection_values(pair: str, draft: Draft | None, chosen: str) -> dict[str, Any]:
    kind = draft.kind.value if draft is not None else chosen.partition("|")[0]
    return {
        "signature": pair,
        "rule": None if draft is None else draft.rule_key,
        "kind": kind if kind in LinkKind else None,
        "evidence": None if draft is None else dict(draft.evidence),
        "reason": RejectionReason.rejected.value,
    }


def _unlink_rejection(session: Session, link: dict[str, Any]) -> dict[str, Any] | None:
    """The rejection that removing this link leaves, or None for a receipt link or a pair that
    is already remembered."""
    if link["kind"] == LinkKind.receipt.value:
        return None
    pair = pair_signature(
        link["from_transaction_id"], link["to_transaction_id"], int(link["amount_cents"])
    )
    if session.scalar(select(RejectedProposal.id).where(RejectedProposal.signature == pair)):
        return None
    evidence = dict(link["evidence"] or {})
    return {
        "signature": pair,
        "rule": evidence.get("rule"),
        "kind": link["kind"],
        "evidence": evidence,
        "reason": RejectionReason.unlinked.value,
    }
