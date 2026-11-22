"""Replies that render one block of a page's fragments template, for htmx to swap in."""

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse


def render_block(
    request: Request,
    template: str,
    block: str,
    *,
    status: int = 200,
    trigger: dict[str, bool] | None = None,
    **context: Any,
) -> HTMLResponse:
    """One block of `template`. trigger names the events the reply raises in the browser."""
    response = request.app.state.templates.TemplateResponse(
        request, template, context, status_code=status, block_name=block
    )
    if trigger:
        response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


def undo_url(batch_id: int | None, **params: Any) -> str | None:
    """Where a toast's Undo posts for `batch_id`, with the page's parameters that are set, or None
    when there is no batch to undo."""
    if not batch_id:
        return None
    query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
    return f"/batches/{batch_id}/undo" + (f"?{query}" if query else "")
