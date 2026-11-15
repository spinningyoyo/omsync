"""Replies shared by the Categories routes: fragments of the page, the events a change raises, the
toast that names its batch, a draft's preview, and the reading of form text that every feature
repeats.

A keyword or rule change is followed in the same request by the automatic step (see
omsync.web.automatic), which categorizes again so categories follow it at once. applied_toast says
what the step changed, and undo reverses the change together with the step.

The routes are split by feature: categories.py is the page, the table, and the icons;
categories_keywords.py, categories_rules.py, and categories_triage.py hold the rest.
"""

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import StalePreview
from omsync.ledger.categories_admin import DraftPreview
from omsync.web.automatic import run_automatic, with_automatic
from omsync.web.fragments import render_block

FRAGMENTS = "categories/fragments.html"
PREVIEW_LIMIT = 50  # the transactions a preview lists; the rest are counted
CHANGED = "categories-changed"


def block(
    request: Request,
    name: str,
    *,
    status: int = 200,
    trigger: dict[str, bool] | None = None,
    **context: Any,
) -> HTMLResponse:
    """One block of the fragments template, with the events to raise in the browser."""
    return render_block(request, FRAGMENTS, name, status=status, trigger=trigger, **context)


def events(*, changed: bool = False, close: bool = False) -> dict[str, bool]:
    result: dict[str, bool] = {}
    if changed:
        result[CHANGED] = True
    if close:
        result["close-dialogs"] = True
    return result


def refused(exc: Exception) -> int:
    """409 when the data moved on since the form was made, 422 for anything the form can fix."""
    return 409 if isinstance(exc, StalePreview) else 422


def toast(batch: Batch, *, label: str | None = None) -> dict[str, Any]:
    """The toast's label and batch. The batch's own label names it unless one is given."""
    summary = batch.summary or {}
    return {"label": label or str(summary.get("label", "Change saved")), "batch_id": batch.id}


def whole_number(text: str, message: str) -> int:
    """`text` as an int that fits a database column, or ValueError(message)."""
    try:
        number = int(text.strip())
    except ValueError:
        raise ValueError(message) from None
    if abs(number) > 2**31 - 1:
        raise ValueError(message)
    return number


def applied_toast(db: Session, batch: Batch, *, label: str | None = None) -> dict[str, Any]:
    """The toast for a keyword or rule change, after the automatic step has run on it. It says what
    the step categorized, and undo reverses the change together with that step. label names the
    change when the batch's own label does not."""
    result = run_automatic(db, batch.id)
    named = label or str((batch.summary or {}).get("label", "Change saved"))
    label = with_automatic(named.rstrip("."), result)
    if result.problem is None and not result.categorized:
        label = f"{label.rstrip('.')}. No transaction's category changed."
    return toast(batch, label=label)


def preview_reply(
    request: Request, preview: DraftPreview | None, error: str = "", note: str = ""
) -> HTMLResponse:
    """A draft's preview: how many transactions it would change, and the first of them. An error
    or a note replaces the list."""
    if error or preview is None:
        return block(
            request,
            "frag_change_preview",
            status=422 if error else 200,
            error=error,
            note=note,
            count=0,
            moves=(),
        )
    return block(
        request,
        "frag_change_preview",
        error="",
        note="",
        count=preview.count,
        moves=preview.changes[:PREVIEW_LIMIT],
    )
