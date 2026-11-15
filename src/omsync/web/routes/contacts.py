"""The Contacts page in Settings: the list of every contact, you first, and each contact's page.

Everything on it is contacts-package data and changes through batches. A write is followed in the
same request by the automatic step (see omsync.web.automatic), and the reply swaps the page's
region. This module has the page, the list's form that names you until you are named, and creating
a contact; the edits of one contact are in contacts_detail.py. Account nicknames are on the account
page (routes/accounts.py).
"""

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError
from omsync.ledger.contacts import (
    add_owner_name,
    create_contact,
    get_owner,
    starting_names,
)
from omsync.web.automatic import run_automatic, with_automatic
from omsync.web.contacts_view import detail_context, list_context, region_context
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/settings_contacts.html"
FRAGMENTS = "settings/contacts_fragments.html"
PATH = "/settings/contacts"


@router.get("/settings/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, db: DbSession) -> HTMLResponse:
    return _page(request, db, "Contacts", list_context(db), PATH)


@router.get("/settings/contacts/{contact_id}", response_class=HTMLResponse)
def contact_page(request: Request, db: DbSession, contact_id: int) -> Response:
    try:
        context = detail_context(db, contact_id)
    except LookupError:
        # A contact that is gone has no page.
        return RedirectResponse(PATH, status_code=303)
    return _page(request, db, "Contacts", context, PATH)


@router.get("/settings/you")
@router.get("/settings/names")
def contacts_redirect(db: DbSession) -> RedirectResponse:
    """The old You pages: your page when you have been named, else the list that names you."""
    owner = get_owner(db)
    return RedirectResponse(f"{PATH}/{owner.id}" if owner else PATH, status_code=303)


@router.post("/settings/contacts", response_class=HTMLResponse)
def create_route(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "person",
    given: Annotated[str, Form()] = "",
    middle: Annotated[str, Form()] = "",
    family: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        batch = create_contact(
            db,
            name=name,
            kind=kind,
            names=starting_names(name, kind, given=given, middle=middle, family=family),
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except (ValueError, BatchError) as exc:
        return error_reply(request, db, str(exc), None)
    created = (batch.summary or {}).get("contact_id")
    return saved_reply(request, db, batch, created if isinstance(created, int) else None)


@router.post("/settings/contacts/you/names", response_class=HTMLResponse)
def add_owner_name_route(
    request: Request,
    db: DbSession,
    given: Annotated[str, Form()] = "",
    middle: Annotated[str, Form()] = "",
    family: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        batch = add_owner_name(
            db,
            given=given,
            middle=middle,
            family=family,
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except (ValueError, BatchError) as exc:
        owner = get_owner(db)
        return error_reply(request, db, str(exc), owner.id if owner else None)
    owner = get_owner(db)
    return saved_reply(request, db, batch, owner.id if owner else None)


# Replies, shared with contacts_detail.py and the undo route


def contacts_undo_reply(
    request: Request, db: Session, contact_id: int | None, message: str = "Undone"
) -> HTMLResponse:
    """After an undo of a contact or nickname batch: the toast, and the region out of band. The
    automatic step runs again, because what it identified rested on what was undone."""
    message = with_automatic(message, run_automatic(db))
    return _fragment(
        request,
        "frag_undo_reply",
        message=message,
        contact_id=contact_id,
        **region_context(db, contact_id),
    )


def saved_reply(
    request: Request, db: Session, batch: Batch, contact_id: int | None
) -> HTMLResponse:
    """The region after a saved write, with the automatic step run and its toast. contact_id is
    the contact left on screen: its detail, or the list when it is None, gone, or the owner."""
    label = str((batch.summary or {}).get("label", "Change saved"))
    message = with_automatic(label, run_automatic(db, batch.id))
    context = region_context(db, contact_id)
    shown = contact_id if context["view"] == "detail" else None
    return _fragment(
        request,
        "frag_saved",
        message=message,
        batch_id=batch.id,
        contact_id=shown,
        **context,
    )


def error_reply(
    request: Request, db: Session, message: str, contact_id: int | None, status: int = 422
) -> HTMLResponse:
    return _fragment(
        request, "frag_error", status=status, error=message, **region_context(db, contact_id)
    )


def _page(
    request: Request, db: Session, title: str, context: dict[str, Any], path: str
) -> HTMLResponse:
    page = shell_context(request, db, active="", title=title)
    page.update(nav=SETTINGS_NAV, active_href=path, page_title=title, **context)
    return request.app.state.templates.TemplateResponse(request, PAGE, page)


def _fragment(request: Request, block: str, *, status: int = 200, **context: Any) -> HTMLResponse:
    response = render_block(request, FRAGMENTS, block, status=status, **context)
    # The region may show a different view than the page was opened on, so the address follows it.
    response.headers["HX-Replace-Url"] = str(context["region_url"])
    return response
