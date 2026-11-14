"""Same-account and cross-account merchant-return proposals."""

import re
from collections import defaultdict

from omsync.ledger.categories import NOT_IN_REPORTS, CategoryType
from omsync.ledger.links import LinkKind
from omsync.ledger.receipt_vendors import is_receipt_merchant
from omsync.reconcile.model import Queue, TxnView
from omsync.reconcile.pool import Pool
from omsync.reconcile.rules.base import Emitter, claim, days_after
from omsync.reconcile.text import is_generic_paypal_transfer, merchant_brand_similarity

NON_SPENDING_TYPES = frozenset({CategoryType.income, *NOT_IN_REPORTS})
"""Category types that never take part in merchant returns: money that is not spending, such as
transfers, loans, investments, and income."""


def is_spending(txn: TxnView) -> bool:
    """True unless the transaction's category is of a type that is not spending."""
    return txn.category_type not in NON_SPENDING_TYPES


MERCHANT_RETURN_WINDOW_DAYS = 30
BRAND_SIMILARITY_THRESHOLD = 0.85
ONE_CENT = 1

# PayPal processor prefix, used by rule_paypal_return_same_account to keep same-account returns
# to PayPal payees.
PAYPAL_RE = re.compile(r"\bPAYPAL\b", re.IGNORECASE)

# Money sent to or received from a person: a Zelle payment, anything through Venmo, or a PayPal
# transfer. A payment from a person is never a store's refund, even when the amounts agree.
_PERSON_PAYMENT_RE = re.compile(
    r"^\s*Zelle\b"
    r"|\bVENMO\b"
    r"|PAYPAL\s+(?:TRANSFER|INST\s*XFER|DES:)"
    r"|PayPal\s+(?:transfer|payment)\s+(?:from|to)\b",
    re.IGNORECASE,
)


def rule_return_same_account(pool: Pool, emit: Emitter) -> None:
    """Same-account merchant return: an exact-amount refund 1 to 30 days after a purchase.

    It is automatic when the two share a brand, and a review proposal otherwise. Purchases with the
    same description are interchangeable, so a refund that fits several of only those takes the
    latest, then the highest ID. A refund that fits several different purchases is proposed for
    manual review and drains nothing.
    """
    by_account_and_amount: dict[tuple[int, int], list[TxnView]] = defaultdict(list)
    for txn in _merchant_candidates(pool):
        by_account_and_amount[(txn.account_id, abs(txn.amount_cents))].append(txn)

    for group in by_account_and_amount.values():
        purchases = [t for t in group if t.amount_cents < 0]
        refunds = [t for t in group if t.amount_cents > 0]
        for refund in refunds:
            if refund.id not in pool:
                continue
            candidates = [
                p
                for p in purchases
                if p.id in pool and _is_return_window(p, refund) and emit.allowed(p, refund)
            ]
            identical_note = None
            if len(candidates) > 1 and len({p.description for p in candidates}) == 1:
                identical_note = f"latest of {len(candidates)} identical purchases"
                candidates = [max(candidates, key=lambda p: (p.posted_on, p.id))]
            same_brand = len(candidates) == 1 and _same_brand(candidates[0], refund)
            _settle_return(
                pool,
                emit,
                rule="return_same_account",
                refund=refund,
                candidates=candidates,
                queue=Queue.auto_approve if same_brand else Queue.review,
                ambiguous_note=(
                    f"ambiguous: {len(candidates)} purchases match; manual review required"
                ),
                match_notes=identical_note,
            )


def rule_paypal_return_same_account(pool: Pool, emit: Emitter) -> None:
    """Same-account PayPal return: a PayPal refund within a cent of a PayPal purchase.

    Both sides must be on the same account, within 30 days, and share a brand.
    """
    paypal = [
        t
        for t in pool
        if is_spending(t)
        and not _is_person_payment(t)
        and PAYPAL_RE.search(t.payee_raw)
        and not is_generic_paypal_transfer(t.payee_raw)
    ]
    purchases = [t for t in paypal if t.amount_cents < 0]
    refunds = [t for t in paypal if t.amount_cents > 0]

    for refund in refunds:
        if refund.id not in pool:
            continue
        candidates = [
            p
            for p in purchases
            if p.id in pool
            and p.account_id == refund.account_id
            and _within_one_cent(p, refund)
            and _is_return_window(p, refund)
            and _same_brand(p, refund)
            and emit.allowed(p, refund)
        ]
        _settle_return(
            pool,
            emit,
            rule="paypal_return_same_account",
            refund=refund,
            candidates=candidates,
            queue=Queue.review,
            ambiguous_note="ambiguous: multiple PayPal debits match",
        )


def rule_return_other_account_same_store(pool: Pool, emit: Emitter) -> None:
    """Cross-account return: a refund within a cent of a purchase on another account.

    The two must share a brand, and the refund must come 1 to 30 days after the purchase.
    """
    merchants = _merchant_candidates(pool)
    purchases = [t for t in merchants if t.amount_cents < 0]
    refunds = [t for t in merchants if t.amount_cents > 0]

    for refund in refunds:
        if refund.id not in pool:
            continue
        candidates = [
            p
            for p in purchases
            if p.id in pool
            and p.account_id != refund.account_id
            and _within_one_cent(p, refund)
            and _is_return_window(p, refund)
            and _same_brand(p, refund)
            and emit.allowed(p, refund)
        ]
        match_notes = None
        if len(candidates) == 1:
            similarity = merchant_brand_similarity(candidates[0].payee_raw, refund.payee_raw)
            match_notes = f"brand_sim:{similarity:.2f}"
        _settle_return(
            pool,
            emit,
            rule="return_other_account_same_store",
            refund=refund,
            candidates=candidates,
            queue=Queue.review,
            ambiguous_note="cross_account_ambiguity",
            match_notes=match_notes,
        )


def is_pipeline_merchant(payee: str | None) -> bool:
    """True for merchants that receipt matching owns (Amazon, Costco), so returns skip them."""
    return is_receipt_merchant(payee)


def _merchant_candidates(pool: Pool) -> list[TxnView]:
    """Spending that a store could refund: not a payment to or from a person, and not a purchase
    that receipt matching owns."""
    return [
        t
        for t in pool
        if is_spending(t) and not _is_person_payment(t) and not is_pipeline_merchant(t.payee_raw)
    ]


def _is_person_payment(txn: TxnView) -> bool:
    return _PERSON_PAYMENT_RE.search(txn.description) is not None


def _same_brand(purchase: TxnView, refund: TxnView) -> bool:
    return (
        merchant_brand_similarity(purchase.payee_raw, refund.payee_raw)
        >= BRAND_SIMILARITY_THRESHOLD
    )


def _settle_return(
    pool: Pool,
    emit: Emitter,
    *,
    rule: str,
    refund: TxnView,
    candidates: list[TxnView],
    queue: Queue,
    ambiguous_note: str,
    match_notes: str | None = None,
) -> None:
    """Propose the refund against its one purchase, or flag it as ambiguous when several fit."""
    if len(candidates) == 1:
        claim(
            pool,
            emit,
            rule=rule,
            kind=LinkKind.refund,
            first=candidates[0],
            second=refund,
            queue=queue,
            notes=match_notes,
        )
    elif len(candidates) > 1:
        claim(
            pool,
            emit,
            rule=rule,
            kind=LinkKind.refund,
            first=candidates[0],
            second=refund,
            queue=Queue.manual,
            notes=ambiguous_note,
        )


def _is_return_window(purchase: TxnView, refund: TxnView) -> bool:
    """A refund comes after its purchase, by at least one day and at most the window."""
    return 0 < days_after(refund, purchase) <= MERCHANT_RETURN_WINDOW_DAYS


def _within_one_cent(first: TxnView, second: TxnView) -> bool:
    return abs(abs(first.amount_cents) - abs(second.amount_cents)) <= ONE_CENT
