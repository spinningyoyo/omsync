"""Replies shared by the Plan routes: the fragment renderer, the amount a form types, and the
status a refused change answers with.

A refused change answers 422 with its message in the form it came from. A change that the data
no longer matches answers 409. Successful changes answer 200 with the regions they changed, out
of band, and a toast whose Undo posts to /batches/{id}/undo?plan=... (see plan.plan_undo_reply).
"""

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from omsync.core.batches import BatchError, StalePreview
from omsync.web.amounts import parse_cents
from omsync.web.fragments import render_block

FRAGMENTS = "plan/fragments.html"
STALE_MESSAGE = "This changed elsewhere, reload."
CONFLICT_MESSAGE = "This change was already made or undone. Reload the page and try again."
CLOSE_DIALOGS = {"close-dialogs": True}


def money_field(text: str, what: str) -> int:
    """An amount a form typed, in cents. Raises ValueError with a message the form can show."""
    return parse_cents(text, f"Enter {what}, such as 250.00.")


def refused(exc: Exception) -> tuple[int, str]:
    """The status and message for a change that was refused."""
    if isinstance(exc, StalePreview):
        return 409, STALE_MESSAGE
    if isinstance(exc, BatchError):
        return 409, CONFLICT_MESSAGE
    text = str(exc) or "That change could not be saved."
    return 422, text[:1].upper() + text[1:]


def fragment(
    request: Request,
    block: str,
    *,
    status_code: int = 200,
    trigger: dict[str, bool] | None = None,
    **context: Any,
) -> HTMLResponse:
    """One block of the Plan fragments, with an optional HX-Trigger for the page's listeners."""
    return render_block(request, FRAGMENTS, block, status=status_code, trigger=trigger, **context)
