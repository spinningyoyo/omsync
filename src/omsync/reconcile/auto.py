"""Automatic reconciliation after an import: apply the proposals that need no review.

reconcile_after_import identifies the other side of every transaction (see
omsync.ledger.other_side), categorizes with the keywords, runs the engine over the whole history,
and records every proposal in
the automatic queue as one batch through decisions.approve_drafts, so stale checks, idempotency,
and undo are the ones every approval has. The whole history is read because a transfer's two sides
often arrive in different imports, and because a wider run sees more competing candidates, which
only ever makes a match less certain. Proposals in the review and manual queues, rejected pairs,
and rules whose links you keep removing are never applied here: the engine leaves them out of the
automatic queue.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch, BatchState
from omsync.core.batches import BatchError, Preview, undo_all
from omsync.ledger.categorize import InvalidKeyword, apply_categorize, plan_categorize
from omsync.ledger.other_side import apply_other_side, plan_other_side
from omsync.ledger.transactions import Transaction
from omsync.reconcile.decisions import approve_drafts
from omsync.reconcile.engine import run
from omsync.reconcile.model import Draft, Queue

BATCH_LABEL = "Automatic reconcile after import"
TRIGGER_KEY = "triggered_by"


@dataclass(frozen=True)
class AutoReconcile:
    """What the automatic step did: the transactions whose other side was identified or changed
    first, the ones the keywords categorized next, and the links it recorded with the batch that
    holds them.

    problem is set, and nothing is linked, when the step could not run. The import it followed is
    not affected.
    """

    linked: int = 0
    batch_id: int | None = None
    problem: str | None = None
    categorized: int = 0
    identified: int = 0


def reconcile_after_import(session: Session, *, triggered_by: int | None = None) -> AutoReconcile:
    """Apply every automatic proposal over the whole history as one batch. With none, write nothing.

    triggered_by is the batch of a change you made that this step follows. Each batch the step
    writes then names it in its summary, so undoing that change can undo them first (see
    undo_with_automatic).

    The other side of each transaction is identified first, because the round-trip rules skip
    payments to the owner and a rule with the person condition reads it. Recognition reads no
    category, so it does not need categorize to go first. Keywords categorize next, because the
    merchant-return rules skip transfers, loans, and investments by category, so rows still unset
    would link as returns. If either fails, nothing is linked.

    Never raises for a failure of its own: the import before it is already committed, so a failure
    is returned as the problem, and Reconcile still offers the proposals.
    """
    categorized = identified = 0
    try:
        first, last = session.execute(
            select(func.min(Transaction.posted_on), func.max(Transaction.posted_on))
        ).one()
        if first is None or last is None:
            return AutoReconcile()
        identifying = plan_other_side(session)
        if identifying.preview is not None:
            apply_other_side(
                session,
                replace(identifying, preview=_tagged(identifying.preview, triggered_by)),
                idempotency_key=f"other-side-before-reconcile:{uuid4()}",
            )
            identified = identifying.changed
        categorizing = plan_categorize(session)
        if categorizing.preview is not None:
            apply_categorize(
                session,
                replace(categorizing, preview=_tagged(categorizing.preview, triggered_by)),
                idempotency_key=f"categorize-before-reconcile:{uuid4()}",
            )
            categorized = categorizing.changed
        drafts = automatic_drafts(run(session, start=first, end=last).drafts)
        if not drafts:
            return AutoReconcile(categorized=categorized, identified=identified)
        noun = "link" if len(drafts) == 1 else "links"
        batch = approve_drafts(
            session,
            drafts,
            idempotency_key=f"reconcile-auto:{uuid4()}",
            label=f"{BATCH_LABEL}: {len(drafts)} {noun}",
            summary=None if triggered_by is None else {TRIGGER_KEY: triggered_by},
        )
    except (BatchError, InvalidKeyword) as exc:
        session.rollback()
        return AutoReconcile(problem=str(exc))
    except Exception as exc:  # the import is already saved; do not fail it for this step
        session.rollback()
        return AutoReconcile(problem=f"unexpected error ({type(exc).__name__})")
    return AutoReconcile(
        linked=len(drafts), batch_id=batch.id, categorized=categorized, identified=identified
    )


def undo_with_automatic(session: Session, batch_id: int) -> tuple[Batch, int]:
    """Undo a batch together with the automatic batches that followed it, as one undo.

    The automatic batches that name batch_id as their trigger are undone first, newest first, then
    batch_id itself. It is all or nothing: if any cannot be undone because later work changed the
    same rows, none is, and the error is the one that batch raised. Returns the batch and how many
    automatic batches were undone with it. A batch with none is undone as core undo does.
    """
    followers = list(
        session.scalars(
            select(Batch.id)
            .where(
                Batch.state == BatchState.applied,
                func.json_extract(Batch.summary, f"$.{TRIGGER_KEY}") == batch_id,
            )
            .order_by(Batch.id.desc())
        )
    )
    undone = undo_all(session, [*followers, batch_id])
    return undone[-1], len(followers)


def _tagged(preview: Preview | None, triggered_by: int | None) -> Preview | None:
    if preview is None or triggered_by is None:
        return preview
    return replace(preview, summary={**preview.summary, TRIGGER_KEY: triggered_by})


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
