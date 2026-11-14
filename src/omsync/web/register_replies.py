"""Replies from the register's routes: fragments, the out-of-band regions that a change re-renders,
and the URL and events that go with them.

A request the register cannot act on raises RegisterError. The handler in app.py answers it with the
message as a fragment, so htmx shows the message where the request was aimed.
"""

import json
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.responses import HTMLResponse

from omsync.web.register_state import RegisterState

FRAGMENTS = "transactions/fragments.html"


class RegisterError(Exception):
    """A request that cannot be done, with a message for you and the status to answer with."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def fragment(
    request: Request, block: str, *, status_code: int = 200, **context: Any
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, context, status_code=status_code, block_name=block
    )


def reply(
    request: Request,
    state: RegisterState,
    rows: list[Any],
    *,
    block: str = "frag_update",
    full: bool = True,
    events: tuple[str, ...] = ("register-reset",),
    url_mode: str = "push",
    focus: str | None = None,
    **extra: Any,
) -> HTMLResponse:
    """A register reply: its regions out of band, and the URL the register now shows. `rows` are
    the state's page.

    url_mode is "push" for a filter change (Back returns to the old filter), "replace" for a search
    keystroke (so typing does not fill the history), or "none" when the URL stays as it is. The
    `register-reset` event clears the bulk selection, because the rows it named were replaced.
    `focus` is the pager control ("prev", "next", or "size") that the swap replaced and that takes
    focus back."""
    response = fragment(
        request,
        block,
        state=state,
        rows=rows,
        full=full,
        focus=focus,
        **extra,
    )
    if url_mode == "push":
        response.headers["HX-Push-Url"] = state.url
    elif url_mode == "replace":
        response.headers["HX-Replace-Url"] = state.url
    response.headers["HX-Trigger"] = json.dumps(dict.fromkeys(events, True))
    return response


def review_items(items: Sequence[Any]) -> list[tuple[Any, str]]:
    """Pair each open review with a fresh idempotency key for its resolve buttons."""
    return [(review, uuid4().hex) for review in items]
