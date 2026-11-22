"""The Rental properties page: the accounts you rent out with their labels, and the categories that
count as rental expenses. Each write is one batch, answered with the whole region and an undo
toast."""

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError
from omsync.ledger.models import Account, Category
from omsync.ledger.rental import (
    get_rental_accounts,
    remove_rental_account,
    set_rental_account,
    set_rental_category,
)
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV, last4_text

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "settings/rental.html"
FRAGMENTS = "settings/rental_fragments.html"
PATH = "/settings/rental"
NO_ACCOUNT = "Choose an account from the list."
NO_CATEGORY = "Choose a category from the list."


@dataclass(frozen=True)
class PropertyRow:
    account_id: int
    label: str
    account: str  # the account's name and ••last4
    edit_key: str
    remove_key: str


@dataclass(frozen=True)
class AccountChoice:
    account_id: int
    text: str


@dataclass(frozen=True)
class CategoryRow:
    id: int
    name: str
    remove_key: str


@dataclass(frozen=True)
class RentalView:
    properties: list[PropertyRow]
    account_choices: list[AccountChoice]  # open accounts that are not properties yet
    categories: list[CategoryRow]
    category_choices: list[CategoryRow]  # visible categories that are not rental categories yet
    add_property_key: str
    add_category_key: str
    message: str = ""  # the reason a change was refused


@router.get(PATH, response_class=HTMLResponse)
def rental_page(request: Request, db: DbSession) -> HTMLResponse:
    context: dict[str, Any] = shell_context(request, db, active="", title="Rental properties")
    context.update(
        nav=SETTINGS_NAV, active_href=PATH, page_title="Rental properties", view=_view(db)
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.post(f"{PATH}/accounts", response_class=HTMLResponse)
def add_property(
    request: Request,
    db: DbSession,
    account_id: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    chosen = _account(db, account_id)
    if chosen is None or chosen.rental_label is not None:
        return _refused(request, db, NO_ACCOUNT)
    return _write(
        request,
        db,
        lambda key: set_rental_account(db, chosen.id, label, idempotency_key=key),
        idempotency_key,
    )


@router.post(f"{PATH}/accounts/{{account_id}}", response_class=HTMLResponse)
def relabel_property(
    request: Request,
    db: DbSession,
    account_id: int,
    label: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if account_id not in get_rental_accounts(db):
        return _refused(request, db, "That account is not a rental property.")
    return _write(
        request,
        db,
        lambda key: set_rental_account(db, account_id, label, idempotency_key=key),
        idempotency_key,
    )


@router.post(f"{PATH}/accounts/{{account_id}}/remove", response_class=HTMLResponse)
def remove_property(
    request: Request,
    db: DbSession,
    account_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Stop treating the account as a rental property. Its transactions are not changed."""
    return _write(
        request,
        db,
        lambda key: remove_rental_account(db, account_id, idempotency_key=key),
        idempotency_key,
    )


@router.post(f"{PATH}/categories", response_class=HTMLResponse)
def add_category(
    request: Request,
    db: DbSession,
    category_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    chosen = _category_id(category_id)
    if chosen is None:
        return _refused(request, db, NO_CATEGORY)
    return _write(
        request,
        db,
        lambda key: set_rental_category(db, chosen, True, idempotency_key=key),
        idempotency_key,
    )


@router.post(f"{PATH}/categories/{{category_id}}/remove", response_class=HTMLResponse)
def remove_category(
    request: Request,
    db: DbSession,
    category_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    return _write(
        request,
        db,
        lambda key: set_rental_category(db, category_id, False, idempotency_key=key),
        idempotency_key,
    )


def rental_undo_reply(request: Request, db: Session, message: str) -> HTMLResponse:
    """After an undo of a rental batch: the toast, and the region out of band."""
    return render_block(request, FRAGMENTS, "frag_undo_reply", message=message, view=_view(db))


def _write(request: Request, db: Session, write: Any, idempotency_key: str) -> HTMLResponse:
    """Run one write, then answer with the region and its undo toast, or with why it was refused."""
    try:
        batch: Batch = write(idempotency_key or uuid4().hex)
    except (LookupError, ValueError, BatchError) as exc:
        return _refused(request, db, str(exc))
    label = str((batch.summary or {}).get("label", "Saved"))
    return render_block(
        request, FRAGMENTS, "frag_rental", view=_view(db), message=label, batch_id=batch.id
    )


def _refused(request: Request, db: Session, message: str) -> HTMLResponse:
    return render_block(
        request, FRAGMENTS, "frag_rental", status=422, view=_view(db, message=message)
    )


def _account(db: Session, raw: str) -> Account | None:
    """The account a form names, or None when the value is not an ID of an existing account."""
    try:
        return db.get(Account, int(raw))
    except ValueError:
        return None


def _category_id(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _view(db: Session, *, message: str = "") -> RentalView:
    accounts = list(db.scalars(select(Account)))
    properties = [
        PropertyRow(
            account_id=account.id,
            label=str(account.rental_label),
            account=_account_text(account),
            edit_key=uuid4().hex,
            remove_key=uuid4().hex,
        )
        for account in accounts
        if account.rental_label is not None
    ]
    properties.sort(key=lambda row: (row.label.casefold(), row.account_id))
    open_accounts = sorted(
        (row for row in accounts if not row.closed and row.rental_label is None),
        key=lambda row: (row.name.casefold(), row.id),
    )
    categories = sorted(db.scalars(select(Category)), key=lambda row: row.name.casefold())
    return RentalView(
        properties=properties,
        account_choices=[AccountChoice(row.id, _account_text(row)) for row in open_accounts],
        categories=[CategoryRow(row.id, row.name, uuid4().hex) for row in categories if row.rental],
        category_choices=[
            CategoryRow(row.id, row.name, "")
            for row in categories
            if not row.rental and not row.hidden
        ],
        add_property_key=uuid4().hex,
        add_category_key=uuid4().hex,
        message=message,
    )


def _account_text(account: Account) -> str:
    last4 = f" {last4_text(account.last4, account.alias_last4)}" if account.last4 else ""
    closed = " (closed)" if account.closed else ""
    return f"{account.name}{last4}{closed}"
