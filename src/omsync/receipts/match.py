"""Matching: which transaction explains each charge of each receipt. Writes nothing.

For each receipt, the vendor says what its charges are. Observed charges are matched to
transactions with the same amount, inside the charge's window, paid by the card whose last four
digits the receipt names. Inferred charges start from candidate transactions the vendor is shown
first. A card no account in this workspace has is paid elsewhere, and is skipped quietly. A charge
that names no card has an unknown payer: it goes to review with the transactions it might be, and
is never applied, because another household member's account could hold the match. Anything else
that does not match exactly once goes to review.

The report lists every outcome. Apply turns the matched ones into changes.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum

from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.orm import Session

from omsync.ledger.categorize import payee_match_text
from omsync.ledger.links import Link, LinkKind
from omsync.ledger.models import Account, Transaction
from omsync.receipts.model import (
    Charge,
    ChargeResult,
    Receipt,
    receipt_ref,
    receipt_revision,
)
from omsync.receipts.vendor import CandidateTxn, ReceiptVendor

# Candidate transactions for inference are posted this many days before or after the receipt.
INFER_DAYS_BEFORE = 3
INFER_DAYS_AFTER = 30


class MatchStatus(StrEnum):
    matched = "matched"
    already_applied = "already_applied"
    changed_since_applied = "changed_since_applied"
    ambiguous = "ambiguous"
    unmatched = "unmatched"
    unknown_payer = "unknown_payer"
    insufficient = "insufficient"
    paid_elsewhere = "paid_elsewhere"


@dataclass(frozen=True)
class MatchOutcome:
    """The result for one charge. receipt_key is the charge key, or the receipt ref when the
    receipt has no charge to match (insufficient). candidate_ids are the transactions an ambiguous
    or unknown-payer charge might be, so review can name them."""

    status: MatchStatus
    receipt: Receipt
    receipt_key: str
    charge: Charge | None
    transaction_id: int | None = None
    candidate_ids: tuple[int, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class MatchReport:
    vendor_key: str
    as_of: date
    outcomes: tuple[MatchOutcome, ...]

    def with_status(self, status: MatchStatus) -> tuple[MatchOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.status is status)

    @property
    def counts(self) -> dict[MatchStatus, int]:
        counts = dict.fromkeys(MatchStatus, 0)
        for outcome in self.outcomes:
            counts[outcome.status] += 1
        return counts


def match_receipts(
    session: Session,
    vendor: ReceiptVendor,
    receipts: Iterable[Receipt],
    *,
    today: date,
) -> MatchReport:
    """Match every charge of every receipt. Transactions dated after today are ignored.

    Raises ValueError for a receipt from another vendor, or one whose revision was not computed
    from its facts (build receipts with make_receipt).
    """
    outcomes: list[MatchOutcome] = []
    for receipt in receipts:
        if receipt.vendor_key != vendor.key:
            raise ValueError(f"receipt {receipt.receipt_id} belongs to {receipt.vendor_key!r}")
        if receipt.revision != receipt_revision(receipt):
            raise ValueError(f"receipt {receipt.receipt_id} has a stale revision")
        outcomes.extend(_receipt_outcomes(session, vendor, receipt, today))
    return MatchReport(vendor.key, today, tuple(_one_claim_per_transaction(outcomes)))


def _receipt_outcomes(
    session: Session, vendor: ReceiptVendor, receipt: Receipt, today: date
) -> list[MatchOutcome]:
    result = vendor.charges(receipt)
    outcomes = _result_outcomes(session, vendor, receipt, result, today)
    if not result.infer_if_unmatched or any(
        outcome.status is not MatchStatus.unmatched for outcome in outcomes
    ):
        return outcomes
    # The stated charge found no transaction at all. Try the receipt as inferred charges, the
    # way an order is matched, and keep the stated outcome if that finds nothing either.
    inferred = _infer(session, vendor, receipt, today)
    if inferred.kind == "observed" and inferred.charges:
        return _charge_outcomes(session, vendor, receipt, inferred.charges, today)
    reason = inferred.reason or "the vendor could not infer charges"
    return [replace(outcome, reason=f"{outcome.reason}; {reason}") for outcome in outcomes]


def _result_outcomes(
    session: Session,
    vendor: ReceiptVendor,
    receipt: Receipt,
    result: ChargeResult,
    today: date,
) -> list[MatchOutcome]:
    if result.kind == "needs_transactions":
        result = _infer(session, vendor, receipt, today)
    if result.kind == "insufficient" or not result.charges:
        reason = result.reason or "the receipt states no charges"
        ref = receipt_ref(receipt.vendor_key, receipt.receipt_id)
        return [MatchOutcome(MatchStatus.insufficient, receipt, ref, None, reason=reason)]
    return _charge_outcomes(session, vendor, receipt, result.charges, today)


def _infer(session: Session, vendor: ReceiptVendor, receipt: Receipt, today: date) -> ChargeResult:
    """Ask the vendor for charges, shown the receipt's candidate transactions."""
    candidates = _inference_candidates(session, vendor, receipt, today)
    result = vendor.infer_charges(receipt, candidates)
    if result.kind == "needs_transactions":
        return ChargeResult("insufficient", reason="the vendor could not infer charges")
    return result


def _charge_outcomes(
    session: Session,
    vendor: ReceiptVendor,
    receipt: Receipt,
    charges: Iterable[Charge],
    today: date,
) -> list[MatchOutcome]:
    return [_match_charge(session, vendor, receipt, charge, today) for charge in charges]


def _match_charge(
    session: Session, vendor: ReceiptVendor, receipt: Receipt, charge: Charge, today: date
) -> MatchOutcome:
    ref = charge.receipt_key
    link = session.scalars(
        select(Link).where(Link.kind == LinkKind.receipt, Link.receipt_key == ref)
    ).first()
    if link is not None:
        if link.evidence.get("revision") != receipt.revision:
            return MatchOutcome(
                MatchStatus.changed_since_applied,
                receipt,
                ref,
                charge,
                link.to_transaction_id,
                reason="the receipt changed after it was applied",
            )
        if charge.transaction_id is not None and link.to_transaction_id != charge.transaction_id:
            return MatchOutcome(
                MatchStatus.changed_since_applied,
                receipt,
                ref,
                charge,
                link.to_transaction_id,
                reason="this charge now comes from a different transaction than the one applied",
            )
        return MatchOutcome(
            MatchStatus.already_applied, receipt, ref, charge, link.to_transaction_id
        )

    if charge.card_last4 is not None and not _workspace_has_last4(session, charge.card_last4):
        return MatchOutcome(
            MatchStatus.paid_elsewhere,
            receipt,
            ref,
            charge,
            reason=f"paid with a card ending {charge.card_last4}, which this workspace lacks",
        )

    window = _window(charge.window_start, charge.window_end, today)
    if charge.card_last4 is None:
        return _unknown_payer(session, vendor, receipt, charge, window)
    if charge.transaction_id is not None:
        return _inferred_match(session, receipt, charge)
    if window is None:
        return MatchOutcome(
            MatchStatus.unmatched, receipt, ref, charge, reason="the charge window has not started"
        )
    clauses = [Transaction.amount_cents == charge.amount_cents, Account.last4 == charge.card_last4]
    rows = _candidate_rows(session, vendor, *window, *clauses)
    if not rows:
        return MatchOutcome(
            MatchStatus.unmatched,
            receipt,
            ref,
            charge,
            reason="no transaction with this amount in the charge window",
        )
    best = _closest(rows, _anchor(receipt, charge))
    if len(best) > 1:
        return MatchOutcome(
            MatchStatus.ambiguous,
            receipt,
            ref,
            charge,
            candidate_ids=tuple(candidate.id for candidate in best),
            reason="more than one transaction fits this charge equally well",
        )
    return MatchOutcome(MatchStatus.matched, receipt, ref, charge, transaction_id=best[0].id)


def _unknown_payer(
    session: Session,
    vendor: ReceiptVendor,
    receipt: Receipt,
    charge: Charge,
    window: tuple[date, date] | None,
) -> MatchOutcome:
    """A charge that names no card cannot say which account paid it, so nothing is applied.

    The closest transactions with its amount in the window, on any tracked account, are listed as
    candidates for a person to review. They are not a match: the search does not decide the payer.
    """
    best: list[CandidateTxn] = []
    if window is not None:
        rows = _candidate_rows(
            session, vendor, *window, Transaction.amount_cents == charge.amount_cents
        )
        best = _closest(rows, _anchor(receipt, charge)) if rows else []
    return MatchOutcome(
        MatchStatus.unknown_payer,
        receipt,
        charge.receipt_key,
        charge,
        candidate_ids=tuple(candidate.id for candidate in best),
        reason="the receipt names no card, so the paying account is unknown",
    )


def _inference_candidates(
    session: Session, vendor: ReceiptVendor, receipt: Receipt, today: date
) -> list[CandidateTxn]:
    """Unlinked transactions a receipt may be paid by, plus the ones this receipt already has.

    The window runs from 3 days before the purchase to 30 days after the purchase or the latest
    ship date, whichever is later. Keeping this receipt's own applied transactions lets a rerun
    rebuild the same charges, so the ones already applied are found again, not lost.
    """
    latest = max((receipt.purchased_on, *receipt.ship_dates))
    window = _window(
        receipt.purchased_on - timedelta(days=INFER_DAYS_BEFORE),
        latest + timedelta(days=INFER_DAYS_AFTER),
        today,
    )
    if window is None:
        return []
    total = abs(receipt.total_cents)
    sign_clause = (
        Transaction.amount_cents > 0 if receipt.is_refund else Transaction.amount_cents < 0
    )
    return _candidate_rows(
        session,
        vendor,
        *window,
        sign_clause,
        func.abs(Transaction.amount_cents) <= total,
        include=_own_transactions(session, receipt),
    )


def _own_transactions(session: Session, receipt: Receipt) -> set[int]:
    """Transactions already applied to this receipt's own charges.

    A charge key is vendor:receipt:<index>. The index must be all digits, so receipt 12 never
    claims the links of receipt 1:2.
    """
    prefix = f"{receipt.vendor_key}:{receipt.receipt_id}:"
    rows = session.execute(
        select(Link.receipt_key, Link.to_transaction_id).where(
            Link.kind == LinkKind.receipt,
            Link.receipt_key.startswith(prefix, autoescape=True),
        )
    ).all()
    # SQL LIKE ignores case, so the exact prefix is checked here too.
    return {
        tx_id
        for key, tx_id in rows
        if key is not None
        and tx_id is not None
        and key.startswith(prefix)
        and key[len(prefix) :].isdigit()
    }


def _inferred_match(session: Session, receipt: Receipt, charge: Charge) -> MatchOutcome:
    """An inferred charge keeps the transaction it was inferred from. It is not searched again.

    The transaction must not already explain another charge of this receipt: a rerun can assign
    charges differently, and such a transaction goes to review rather than being applied twice.
    """
    other_keys = session.scalars(
        select(Link.receipt_key).where(
            Link.kind == LinkKind.receipt, Link.to_transaction_id == charge.transaction_id
        )
    ).all()
    if any(key != charge.receipt_key for key in other_keys):
        return MatchOutcome(
            MatchStatus.unmatched,
            receipt,
            charge.receipt_key,
            charge,
            reason="that transaction already explains another charge of this receipt",
        )
    return MatchOutcome(
        MatchStatus.matched,
        receipt,
        charge.receipt_key,
        charge,
        transaction_id=charge.transaction_id,
    )


def _anchor(receipt: Receipt, charge: Charge) -> date:
    """The date the closest-transaction tie-break measures from."""
    return charge.anchor if charge.anchor is not None else receipt.purchased_on


def _candidate_rows(
    session: Session,
    vendor: ReceiptVendor,
    start: date,
    end: date,
    *clauses: ColumnElement[bool],
    include: Collection[int] = (),
) -> list[CandidateTxn]:
    """Unlinked transactions in the window whose payee the vendor recognizes.

    include adds transactions that are linked already, such as a receipt's own applied charges.
    """
    unlinked = Transaction.id.not_in(_receipt_linked_transactions())
    available = or_(unlinked, Transaction.id.in_(sorted(include))) if include else unlinked
    query = (
        select(
            Transaction.id,
            Transaction.posted_on,
            Transaction.amount_cents,
            Transaction.payee,
            Transaction.payee_raw,
            Account.last4,
        )
        .join(Account, Account.id == Transaction.account_id)
        .where(
            available,
            Transaction.posted_on >= start,
            Transaction.posted_on <= end,
            *clauses,
        )
        .order_by(Transaction.posted_on, Transaction.id)
    )
    rows: list[CandidateTxn] = []
    for row in session.execute(query).all():
        payee = payee_match_text(row.payee_raw, row.payee)
        if vendor.is_vendor_payee(payee):
            rows.append(CandidateTxn(row.id, row.posted_on, row.amount_cents, payee, row.last4))
    return rows


def _receipt_linked_transactions() -> Select:
    return select(Link.to_transaction_id).where(
        Link.kind == LinkKind.receipt, Link.to_transaction_id.is_not(None)
    )


def _workspace_has_last4(session: Session, last4: str) -> bool:
    found = session.scalars(select(Account.id).where(Account.last4 == last4).limit(1)).first()
    return found is not None


def _window(start: date, end: date, today: date) -> tuple[date, date] | None:
    """The window with its end capped at today, or None when nothing in it can have posted."""
    end = min(end, today)
    return None if start > end else (start, end)


def _closest(candidates: list[CandidateTxn], anchor: date) -> list[CandidateTxn]:
    """The candidates tied at the smallest distance from the anchor date."""
    distance = {c.id: abs((c.posted_on - anchor).days) for c in candidates}
    best = min(distance.values())
    return [c for c in candidates if distance[c.id] == best]


def _one_claim_per_transaction(outcomes: list[MatchOutcome]) -> list[MatchOutcome]:
    """If two charges match the same transaction, neither may be applied. Both go to review."""
    claims: dict[int, int] = {}
    for outcome in outcomes:
        if outcome.status is MatchStatus.matched and outcome.transaction_id is not None:
            claims[outcome.transaction_id] = claims.get(outcome.transaction_id, 0) + 1
    resolved: list[MatchOutcome] = []
    for original in outcomes:
        outcome = original
        tx_id = outcome.transaction_id
        if outcome.status is MatchStatus.matched and tx_id is not None and claims[tx_id] > 1:
            outcome = replace(
                outcome,
                status=MatchStatus.ambiguous,
                candidate_ids=(tx_id,),
                reason="another receipt charge also matches this transaction",
            )
        resolved.append(outcome)
    return resolved
