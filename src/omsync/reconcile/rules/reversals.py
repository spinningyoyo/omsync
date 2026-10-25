"""Same-account returns: bank reversals, and round trips through an untracked account."""

import re
from collections import Counter, defaultdict

from omsync.ledger.links import LinkKind
from omsync.reconcile.model import Queue, TxnView
from omsync.reconcile.pool import Pool
from omsync.reconcile.rules.base import Emitter, account_mentions, claim, days_gap
from omsync.reconcile.rules.transfers import is_own_inflow, is_transfer_like

REVERSAL_WINDOW_DAYS = 10
UNTRACKED_ROUND_TRIP_WINDOW_DAYS = 30

_REVERSAL_RE = re.compile(r"^\s*REVERSAL\b", re.IGNORECASE)

# A reference is six or more digits that are not part of a longer number: a transaction #, a
# Conf# or a similar number a bank prints on both the original and its reversal.
_REFERENCE_RE = re.compile(r"(?<!\d)\d{6,}(?!\d)")


def rule_reversals(pool: Pool, emit: Emitter) -> None:
    """Bank reversal: a REVERSAL line and the transaction it undoes, in the same account.

    The two share a reference of six or more digits, have exactly opposite amounts, and post
    within 10 days of each other. One original for one reversal is automatic. A reversal with
    several originals, or an original with several reversals, is proposed for review as the
    nearest pair.
    """
    open_txns = list(pool)
    reversals = [t for t in open_txns if _REVERSAL_RE.search(t.description)]
    originals = [t for t in open_txns if not _REVERSAL_RE.search(t.description)]
    fitting = {
        reversal.id: [o for o in originals if _fits(reversal, o) and emit.allowed(reversal, o)]
        for reversal in reversals
    }
    reversals_per_original = Counter(o.id for found in fitting.values() for o in found)

    for reversal in reversals:
        candidates = [o for o in fitting[reversal.id] if o.id in pool]
        if reversal.id not in pool or not candidates:
            continue
        original = min(candidates, key=lambda o: (days_gap(reversal, o), o.id))
        unique = len(fitting[reversal.id]) == 1 and reversals_per_original[original.id] == 1
        claim(
            pool,
            emit,
            rule="rv",
            kind=LinkKind.refund,
            first=reversal,
            second=original,
            queue=Queue.auto_approve if unique else Queue.review,
            conf_number=_shared_reference(reversal, original),
            notes=None
            if unique
            else f"ambiguous: {len(fitting[reversal.id])} originals, "
            f"{reversals_per_original[original.id]} reversals",
        )


def rule_untracked_round_trip(pool: Pool, emit: Emitter) -> None:
    """Round trip through an untracked account: money sent to it and back, on one account.

    Both descriptions read as transfers and name the same last four digits, which belong to no
    tracked account (see account_mentions). A masked number in other text, such as a company ID
    or a card number on a purchase, is not a reference. The amounts are exactly opposite and the
    dates at most 30 days apart, in either order. Every pair is automatic: the payments share an
    account, an amount, and the other account, so of several, the nearest pairs are linked first,
    by date gap and then IDs. Payments whose stored other side is the owner or the co-owner are left
    out, as in the P2P round trip.
    """
    tracked = {digits for account in emit.accounts for digits in account.digits}
    by_reference: dict[tuple[int, int, str], list[TxnView]] = defaultdict(list)
    for txn in pool:
        reads_as_transfer = (
            is_transfer_like(txn, emit) if txn.amount_cents < 0 else is_own_inflow(txn, emit)
        )
        if not reads_as_transfer or txn.with_household:
            continue
        for digits in account_mentions(txn.description) - tracked:
            by_reference[(txn.account_id, abs(txn.amount_cents), digits)].append(txn)

    pairs = sorted(
        (
            (out, inn, digits)
            for (_, _, digits), group in by_reference.items()
            for out in group
            if out.amount_cents < 0
            for inn in group
            if inn.amount_cents > 0
            and days_gap(out, inn) <= UNTRACKED_ROUND_TRIP_WINDOW_DAYS
            and emit.allowed(out, inn)
        ),
        key=lambda pair: (days_gap(pair[0], pair[1]), pair[0].id, pair[1].id),
    )
    for out, inn, digits in pairs:
        if out.id in pool and inn.id in pool:
            claim(
                pool,
                emit,
                rule="ru",
                kind=LinkKind.refund,
                first=out,
                second=inn,
                queue=Queue.auto_approve,
                extra={"untracked_account": digits},
            )


def _fits(reversal: TxnView, original: TxnView) -> bool:
    return (
        reversal.account_id == original.account_id
        and reversal.amount_cents + original.amount_cents == 0
        and days_gap(reversal, original) <= REVERSAL_WINDOW_DAYS
        and _shared_reference(reversal, original) is not None
    )


def _shared_reference(first: TxnView, second: TxnView) -> str | None:
    shared = set(_REFERENCE_RE.findall(first.description)) & set(
        _REFERENCE_RE.findall(second.description)
    )
    return min(shared) if shared else None
