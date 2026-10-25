"""Edits of one contact: its names, patterns, and accounts, and its name, kind, role, deletion, and
merge into another contact. The page is in contacts.py.

Each route calls one domain function, and each form re-renders the contact's page, yours included.
The ID in a remove route's form says which page to show; it is not authority for anything.
"""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.ledger.contacts import (
    ContactRole,
    add_contact_account,
    add_name,
    add_pattern,
    delete_contact,
    merge_contacts,
    remove_contact_account,
    remove_name,
    remove_pattern,
    set_contact_role,
    update_contact,
)
from omsync.web.deps import get_db
from omsync.web.routes.contacts import error_reply, saved_reply

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
ROLE_CHOICES = (ContactRole.other.value, ContactRole.co_owner.value)
Key = Annotated[str, Form()]
Shown = Annotated[int | None, Form()]


def _key(value: str) -> str:
    return value or uuid4().hex


@router.post("/settings/contacts/{contact_id}/edit", response_class=HTMLResponse)
def edit_route(
    request: Request,
    db: DbSession,
    contact_id: int,
    name: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
    idempotency_key: Key = "",
) -> HTMLResponse:
    try:
        batch = update_contact(
            db,
            contact_id,
            name=name or None,
            kind=kind or None,
            idempotency_key=_key(idempotency_key),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such contact") from None
    except (ValueError, BatchError) as exc:
        return error_reply(request, db, str(exc), contact_id)
    return saved_reply(request, db, batch, contact_id)


@router.post("/settings/contacts/{contact_id}/role", response_class=HTMLResponse)
def role_route(
    request: Request,
    db: DbSession,
    contact_id: int,
    role: Annotated[str, Form()] = "",
    idempotency_key: Key = "",
) -> HTMLResponse:
    if role not in ROLE_CHOICES:
        return error_reply(request, db, "Choose other or co-owner.", contact_id)
    try:
        batch = set_contact_role(db, contact_id, role, idempotency_key=_key(idempotency_key))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such contact") from None
    except (ValueError, BatchError) as exc:
        return error_reply(request, db, str(exc), contact_id)
    return saved_reply(request, db, batch, contact_id)


@router.post("/settings/contacts/{contact_id}/delete", response_class=HTMLResponse)
def delete_route(
    request: Request, db: DbSession, contact_id: int, idempotency_key: Key = ""
) -> HTMLResponse:
    try:
        batch = delete_contact(db, contact_id, idempotency_key=_key(idempotency_key))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such contact") from None
    except BatchError as exc:
        return error_reply(request, db, str(exc), contact_id)
    return saved_reply(request, db, batch, None)


@router.post("/settings/contacts/{contact_id}/merge", response_class=HTMLResponse)
def merge_route(
    request: Request,
    db: DbSession,
    contact_id: int,
    into: Annotated[int | None, Form()] = None,
    idempotency_key: Key = "",
) -> HTMLResponse:
    """Merge this contact into another, which stays."""
    if into is None:
        return error_reply(request, db, "Choose the contact to merge into.", contact_id)
    try:
        batch = merge_contacts(
            db, keep_id=into, merge_id=contact_id, idempotency_key=_key(idempotency_key)
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such contact") from None
    except (ValueError, BatchError) as exc:
        return error_reply(request, db, str(exc), contact_id)
    return saved_reply(request, db, batch, into)


@router.post("/settings/contacts/{contact_id}/names", response_class=HTMLResponse)
def add_name_route(
    request: Request,
    db: DbSession,
    contact_id: int,
    given: Annotated[str, Form()] = "",
    middle: Annotated[str, Form()] = "",
    family: Annotated[str, Form()] = "",
    idempotency_key: Key = "",
) -> HTMLResponse:
    try:
        batch = add_name(
            db,
            contact_id,
            given=given,
            middle=middle,
            family=family,
            idempotency_key=_key(idempotency_key),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such contact") from None
    except (ValueError, BatchError) as exc:
        return error_reply(request, db, str(exc), contact_id)
    return saved_reply(request, db, batch, contact_id)


@router.post("/settings/contacts/names/{name_id}/remove", response_class=HTMLResponse)
def remove_name_route(
    request: Request, db: DbSession, name_id: int, contact: Shown = None, idempotency_key: Key = ""
) -> HTMLResponse:
    try:
        batch = remove_name(db, name_id, idempotency_key=_key(idempotency_key))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such name") from None
    except BatchError as exc:
        return error_reply(request, db, str(exc), contact)
    return saved_reply(request, db, batch, contact)


@router.post("/settings/contacts/{contact_id}/patterns", response_class=HTMLResponse)
def add_pattern_route(
    request: Request,
    db: DbSession,
    contact_id: int,
    side: Annotated[str, Form()] = "",
    pattern: Annotated[str, Form()] = "",
    account: Annotated[int | None, Form()] = None,
    idempotency_key: Key = "",
) -> HTMLResponse:
    try:
        batch = add_pattern(
            db,
            contact_id,
            side=side,
            pattern=pattern,
            contact_account_id=account,
            idempotency_key=_key(idempotency_key),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such contact") from None
    except (ValueError, BatchError) as exc:
        return error_reply(request, db, str(exc), contact_id)
    return saved_reply(request, db, batch, contact_id)


@router.post("/settings/contacts/patterns/{pattern_id}/remove", response_class=HTMLResponse)
def remove_pattern_route(
    request: Request,
    db: DbSession,
    pattern_id: int,
    contact: Shown = None,
    idempotency_key: Key = "",
) -> HTMLResponse:
    try:
        batch = remove_pattern(db, pattern_id, idempotency_key=_key(idempotency_key))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such pattern") from None
    except BatchError as exc:
        return error_reply(request, db, str(exc), contact)
    return saved_reply(request, db, batch, contact)


@router.post("/settings/contacts/{contact_id}/accounts", response_class=HTMLResponse)
def add_account_route(
    request: Request,
    db: DbSession,
    contact_id: int,
    label: Annotated[str, Form()] = "",
    last4: Annotated[str, Form()] = "",
    idempotency_key: Key = "",
) -> HTMLResponse:
    try:
        batch = add_contact_account(
            db,
            contact_id,
            label=label,
            last4=last4 or None,
            idempotency_key=_key(idempotency_key),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such contact") from None
    except (ValueError, BatchError) as exc:
        return error_reply(request, db, str(exc), contact_id)
    return saved_reply(request, db, batch, contact_id)


@router.post("/settings/contacts/accounts/{account_id}/remove", response_class=HTMLResponse)
def remove_account_route(
    request: Request,
    db: DbSession,
    account_id: int,
    contact: Shown = None,
    idempotency_key: Key = "",
) -> HTMLResponse:
    try:
        batch = remove_contact_account(db, account_id, idempotency_key=_key(idempotency_key))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such account") from None
    except BatchError as exc:
        return error_reply(request, db, str(exc), contact)
    return saved_reply(request, db, batch, contact)
