"""The automatic step after a write that can change who is on the other side.

A change to contacts, names, patterns, accounts, nicknames, or a transaction's other side is
followed in the same request by the step that runs after an import: it identifies the other side
again, categorizes, and records the links that need no review, each in a batch of its own (see
omsync.reconcile.auto). The toast says what the change did and what the step did after it.
"""

from sqlalchemy.orm import Session

from omsync.reconcile.auto import AutoReconcile, reconcile_after_import


def run_automatic(db: Session, triggered_by: int | None = None) -> AutoReconcile:
    """Run the step. triggered_by is the batch of the change it follows, which the step's own
    batches record so that undoing the change undoes them too. It never raises for a failure of
    its own: the change that came before is saved, and a failure comes back as the result's
    problem."""
    return reconcile_after_import(db, triggered_by=triggered_by)


def with_automatic(message: str, result: AutoReconcile) -> str:
    """The toast text: the change, then what the automatic step did after it."""
    if result.problem is not None:
        return f"{message}. The automatic step could not run: {result.problem}"
    parts = []
    if result.identified:
        parts.append(f"identified the other side of {_count(result.identified, 'transaction')}")
    if result.linked:
        parts.append(f"linked {_count(result.linked, 'pair')}")
    if result.categorized:
        parts.append(f"categorized {_count(result.categorized, 'transaction')}")
    if not parts:
        return message
    return f"{message}. The automatic step {' and '.join(parts)}."


def _count(number: int, noun: str) -> str:
    plural = "pairs" if noun == "pair" else f"{noun}s"
    return f"{number} {noun if number == 1 else plural}"
