"""Automatic reconciliation after an import: apply the proposals that need no review.

reconcile_after_import runs the engine over the whole history and records every proposal in the
automatic queue as one batch through decisions.approve_drafts, so stale checks, idempotency, and
undo are the ones every approval has. The whole history is read because a transfer's two sides
often arrive in different imports, and because a wider run sees more competing candidates, which
only ever makes a match less certain. Proposals in the review and manual queues, rejected pairs,
and rules whose links you keep removing are never applied here: the engine leaves them out of the
automatic queue.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.ledger.categorize import InvalidKeyword, apply_categorize, plan_categorize
from omsync.ledger.transactions import Transaction
from omsync.reconcile.decisions import approve_drafts
from omsync.reconcile.engine import run
from omsync.reconcile.model import Draft, Queue

BATCH_LABEL = "Automatic reconcile after import"


@dataclass(frozen=True)
class AutoReconcile:
    """What the automatic step did: the transactions the keywords categorized first, and the links
    it recorded with the batch that holds them.

    problem is set, and nothing is linked, when the step could not run. The import it followed is
    not affected.
    """

    linked: int = 0
    batch_id: int | None = None
    problem: str | None = None
    categorized: int = 0


def reconcile_after_import(session: Session) -> AutoReconcile:
    """Apply every automatic proposal over the whole history as one batch. With none, write nothing.

    Keywords categorize first: the merchant-return rules skip transfers, loans, and investments by
    category, so rows still unset would link as returns. If that fails, nothing is linked.

    Never raises for a failure of its own: the import before it is already committed, so a failure
    is returned as the problem, and Reconcile still offers the proposals.
    """
    categorized = 0
    try:
        first, last = session.execute(
            select(func.min(Transaction.posted_on), func.max(Transaction.posted_on))
        ).one()
        if first is None or last is None:
            return AutoReconcile()
        categorizing = plan_categorize(session)
        if categorizing.preview is not None:
            apply_categorize(
                session, categorizing, idempotency_key=f"categorize-before-reconcile:{uuid4()}"
            )
            categorized = categorizing.changed
        drafts = automatic_drafts(run(session, start=first, end=last).drafts)
        if not drafts:
            return AutoReconcile(categorized=categorized)
        noun = "link" if len(drafts) == 1 else "links"
        batch = approve_drafts(
            session,
            drafts,
            idempotency_key=f"reconcile-auto:{uuid4()}",
            label=f"{BATCH_LABEL}: {len(drafts)} {noun}",
        )
    except (BatchError, InvalidKeyword) as exc:
        session.rollback()
        return AutoReconcile(problem=str(exc))
    except Exception as exc:  # the import is already saved; do not fail it for this step
        session.rollback()
        return AutoReconcile(problem=f"unexpected error ({type(exc).__name__})")
    return AutoReconcile(linked=len(drafts), batch_id=batch.id, categorized=categorized)


def automatic_drafts(drafts: Sequence[Draft]) -> list[Draft]:
    """The automatic-queue drafts that can be applied together.

    Two automatic drafts that use one transaction cannot both be applied. Neither is: which one
    belongs is a decision for you, and the next run proposes whatever remains.
    """
    chosen = [draft for draft in drafts if draft.queue is Queue.auto_approve]
    uses = Counter(
        txn_id
        for draft in chosen
        for txn_id in (draft.out_txn_id, draft.in_txn_id)
        if txn_id is not None
    )
    return [
        draft
        for draft in chosen
        if all(uses[txn_id] == 1 for txn_id in (draft.out_txn_id, draft.in_txn_id) if txn_id)
    ]
