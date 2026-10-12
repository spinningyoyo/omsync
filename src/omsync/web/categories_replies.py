"""Replies shared by the Categories routes: fragments of the page, the events a change raises, the
toast that names its batch, and the reading of form text that every feature repeats.

The routes are split by feature: categories.py is the page, the table, and the icons;
categories_keywords.py, categories_rules.py, and categories_triage.py hold the rest.
"""

import json
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from omsync.core.batch_models import Batch
from omsync.core.batches import StalePreview

FRAGMENTS = "categories/fragments.html"
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
    response = request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, context, status_code=status, block_name=name
    )
    if trigger:
        response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


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
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError(message) from None
