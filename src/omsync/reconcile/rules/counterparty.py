"""Reimbursement proposals using saved counterparty patterns."""

from omsync.ledger.links import LinkKind
from omsync.reconcile.model import Queue, TxnView
from omsync.reconcile.pool import Pool
from omsync.reconcile.rules.base import Emitter, days_after, pair_unambiguously, within_one_cent

WINDOW_BEFORE_DAYS = 2
WINDOW_AFTER_DAYS = 30


def rule_counterparties(pool: Pool, emit: Emitter) -> None:
    """Pair outflows to each counterparty with its inflows, one counterparty at a time."""
    for matcher in emit.counterparties:
        debits = [t for t in pool if t.amount_cents < 0 and matcher.is_paid_to(t.description)]
        credits = [t for t in pool if t.amount_cents > 0 and matcher.is_paid_by(t.description)]
        pair_unambiguously(
            pool,
            emit,
            rule="cp",
            kind=LinkKind.p2p,
            debits=debits,
            credits=credits,
            fits=_counterparty_fits,
            queue=Queue.review,
            extra={"counterparty_id": matcher.id, "counterparty": matcher.name},
        )


def _counterparty_fits(debit: TxnView, credit: TxnView) -> bool:
    return (
        within_one_cent(debit, credit)
        and -WINDOW_BEFORE_DAYS <= days_after(credit, debit) <= WINDOW_AFTER_DAYS
    )
