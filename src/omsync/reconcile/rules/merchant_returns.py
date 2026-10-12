"""Same-account and cross-account merchant-return proposals."""

import re
from collections import defaultdict

from omsync.ledger.links import LinkKind
from omsync.receipts.vendors import BUILT_IN_VENDORS
from omsync.reconcile.model import Queue, TxnView
from omsync.reconcile.pool import Pool
from omsync.reconcile.rules.base import Emitter, claim, days_after
from omsync.reconcile.text import is_generic_paypal_transfer, merchant_brand_similarity

MERCHANT_EXCLUDED_CATEGORIES = frozenset(
    {
        "Transfer",
        "Transfer_External",
        "Loan",
        "Income_Salary",
        "Income_Rental",
        "Income_Other",
        "Income_Tax",
        "Investment",
        "Suspense",
        "Uncategorized",
    }
)
"""Category names that never take part in merchant returns.

These match the category names this user keeps, so a renamed category must be renamed here too.
"""

MERCHANT_RETURN_WINDOW_DAYS = 30
BRAND_SIMILARITY_THRESHOLD = 0.85
ONE_CENT = 1

# PayPal processor prefix, used by rule 7c to keep same-account returns to PayPal payees.
PAYPAL_RE = re.compile(r"\bPAYPAL\b", re.IGNORECASE)


def rule_7a(pool: Pool, emit: Emitter) -> None:
    """Same-account merchant return: an exact-amount refund 1 to 30 days after a purchase.

    A refund that fits several purchases is proposed for manual review and drains nothing.
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
            _settle_return(
                pool,
                emit,
                rule="7a",
                refund=refund,
                candidates=candidates,
                queue=Queue.auto_approve,
                ambiguous_note=(
                    f"ambiguous: {len(candidates)} purchases match; manual review required"
                ),
            )


def rule_7c(pool: Pool, emit: Emitter) -> None:
    """Same-account PayPal return: a PayPal refund within a cent of a PayPal purchase.

    Both sides must be on the same account, within 30 days, and share a brand.
    """
    paypal = [
        t
        for t in pool
        if (t.category_name or "") not in MERCHANT_EXCLUDED_CATEGORIES
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
            and merchant_brand_similarity(p.payee_raw, refund.payee_raw)
            >= BRAND_SIMILARITY_THRESHOLD
            and emit.allowed(p, refund)
        ]
        _settle_return(
            pool,
            emit,
            rule="7c",
            refund=refund,
            candidates=candidates,
            queue=Queue.review,
            ambiguous_note="ambiguous: multiple PayPal debits match",
        )


def rule_7b(pool: Pool, emit: Emitter) -> None:
    """Cross-account merchant return: a refund within a cent of a purchase on another account.

    The merchant must match, and the refund must come 1 to 30 days after the purchase.
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
            and _same_merchant(p, refund)
            and emit.allowed(p, refund)
        ]
        _settle_return(
            pool,
            emit,
            rule="7b",
            refund=refund,
            candidates=candidates,
            queue=Queue.review,
            ambiguous_note="cross_account_ambiguity",
        )


def rule_7d(pool: Pool, emit: Emitter) -> None:
    """Cross-account return by brand: as 7b, but the brand words must match, not the merchant."""
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
            and merchant_brand_similarity(p.payee_raw, refund.payee_raw)
            >= BRAND_SIMILARITY_THRESHOLD
            and emit.allowed(p, refund)
        ]
        match_notes = None
        if len(candidates) == 1:
            similarity = merchant_brand_similarity(candidates[0].payee_raw, refund.payee_raw)
            match_notes = f"brand_sim:{similarity:.2f}"
        _settle_return(
            pool,
            emit,
            rule="7d",
            refund=refund,
            candidates=candidates,
            queue=Queue.review,
            ambiguous_note="cross_account_ambiguity",
            match_notes=match_notes,
        )


def is_pipeline_merchant(payee: str | None) -> bool:
    """True for merchants that receipt matching owns (Amazon, Costco), so returns skip them."""
    text = payee or ""
    return any(vendor.is_vendor_payee(text) for vendor in BUILT_IN_VENDORS)


def _merchant_candidates(pool: Pool) -> list[TxnView]:
    return [
        t
        for t in pool
        if (t.category_name or "") not in MERCHANT_EXCLUDED_CATEGORIES
        and not is_pipeline_merchant(t.payee_raw)
    ]


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


def _same_merchant(first: TxnView, second: TxnView) -> bool:
    key = _normalized_merchant(first.payee)
    return key is not None and key == _normalized_merchant(second.payee)


def _normalized_merchant(payee: str) -> str | None:
    words = payee.split()
    return words[0].upper() if words else None
