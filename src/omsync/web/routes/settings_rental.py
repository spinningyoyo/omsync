"""Rental account labels and category preference responses."""

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.ledger.models import Account, Category
from omsync.reports.rental import (
    get_rental_accounts,
    get_rental_categories,
    remove_rental_account,
    set_rental_account,
    set_rental_categories,
)
from omsync.web.deps import get_db
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV, last4_text

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "settings/rental.html"
FRAGMENTS = "settings/rental_fragments.html"
PATH = "/settings/rental"
BLANK_LABEL = "Give the property a label."
NO_ACCOUNT = "Choose an account from the list."
NOT_A_PROPERTY = "That account is not a rental property."
NO_CATEGORY = "Choose a category from the list."
NOT_A_RENTAL_CATEGORY = "That category is not a rental category."
LAST_CATEGORY = "Keep at least one rental category."


@dataclass(frozen=True)
class PropertyRow:
    account_id: int
    label: str
    account: str  # the account's name and ••last4, or a note when the account is gone


@dataclass(frozen=True)
class AccountChoice:
    account_id: int
    text: str


@dataclass(frozen=True)
class CategoryChip:
    name: str
    exists: bool  # False when no category has this name yet


@dataclass(frozen=True)
class RentalView:
    properties: list[PropertyRow]
    account_choices: list[AccountChoice]  # open accounts that are not properties yet
    categories: list[CategoryChip]
    category_choices: list[str]  # visible categories that are not rental categories yet
    message: str = ""  # the reason a change was refused
    error: bool = False
    saved: bool = False  # a change was saved, so the reply shows the Saved toast


# Pages


@router.get("/settings/rental", response_class=HTMLResponse)
def rental_page(request: Request, db: DbSession) -> HTMLResponse:
    context: dict[str, Any] = shell_context(request, db, active="", title="Rental properties")
    context.update(
        nav=SETTINGS_NAV,
        active_href=PATH,
        page_title="Rental properties",
        view=_view(db),
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


# Writes. Each one answers with the whole region, so the list and the choices stay in step.


@router.post("/settings/rental/accounts", response_class=HTMLResponse)
def add_property(
    request: Request,
    db: DbSession,
    account_id: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",
) -> HTMLResponse:
    account = _account(db, account_id)
    if account is None:
        return _reply(request, db, error=NO_ACCOUNT, status=422)
    return _label(request, db, account.id, label)


@router.post("/settings/rental/accounts/{account_id}", response_class=HTMLResponse)
def relabel_property(
    request: Request,
    db: DbSession,
    account_id: int,
    label: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if account_id not in get_rental_accounts(db):
        return _reply(request, db, error=NOT_A_PROPERTY, status=422)
    return _label(request, db, account_id, label)


@router.post("/settings/rental/accounts/{account_id}/remove", response_class=HTMLResponse)
def remove_property(request: Request, db: DbSession, account_id: int) -> HTMLResponse:
    """Stop treating the account as a rental property. Its transactions are not changed."""
    if not remove_rental_account(db, account_id):
        return _reply(request, db, error=NOT_A_PROPERTY, status=422)
    return _reply(request, db, saved=True)


@router.post("/settings/rental/categories/add", response_class=HTMLResponse)
def add_category(
    request: Request, db: DbSession, name: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Add a category you already have to the rental categories."""
    chosen = name.strip()
    offered = select(Category.id).where(Category.name == chosen, Category.hidden.is_(False))
    if not chosen or db.scalar(offered) is None:
        return _reply(request, db, error=NO_CATEGORY, status=422)
    set_rental_categories(db, [*get_rental_categories(db), chosen])
    return _reply(request, db, saved=True)


@router.post("/settings/rental/categories/remove", response_class=HTMLResponse)
def remove_category(
    request: Request, db: DbSession, name: Annotated[str, Form()] = ""
) -> HTMLResponse:
    current = get_rental_categories(db)
    chosen = name.strip()
    if chosen not in current:
        return _reply(request, db, error=NOT_A_RENTAL_CATEGORY, status=422)
    kept = [saved for saved in current if saved != chosen]
    if not kept:
        return _reply(request, db, error=LAST_CATEGORY, status=422)
    set_rental_categories(db, kept)
    return _reply(request, db, saved=True)


# Helpers


def _label(request: Request, db: Session, account_id: int, label: str) -> HTMLResponse:
    """Set a property's label. A blank label is refused before anything is written."""
    name = label.strip()
    if not name:
        return _reply(request, db, error=BLANK_LABEL, status=422)
    try:
        set_rental_account(db, account_id, name)
    except ValueError as exc:  # the account vanished between the check and the write
        return _reply(request, db, error=str(exc), status=422)
    return _reply(request, db, saved=True)


def _account(db: Session, raw: str) -> Account | None:
    """The account a form names, or None when the value is not an ID of an existing account."""
    try:
        account_id = int(raw)
    except ValueError:
        return None
    return db.get(Account, account_id)


def _reply(
    request: Request,
    db: Session,
    *,
    error: str = "",
    status: int = 200,
    saved: bool = False,
) -> HTMLResponse:
    view = _view(db, message=error, error=bool(error), saved=saved)
    return request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, {"view": view}, status_code=status, block_name="frag_rental"
    )


def _view(
    db: Session, *, message: str = "", error: bool = False, saved: bool = False
) -> RentalView:
    accounts = get_rental_accounts(db)  # account ID to label, in the order they were added
    found = {row.id: row for row in db.scalars(select(Account))}
    properties = [
        PropertyRow(
            account_id=account_id,
            label=label,
            account=_account_text(found.get(account_id)),
        )
        for account_id, label in sorted(accounts.items(), key=lambda item: item[1].casefold())
    ]
    open_accounts = sorted(
        (row for row in found.values() if not row.closed and row.id not in accounts),
        key=lambda row: (row.name.casefold(), row.id),
    )
    chosen = get_rental_categories(db)
    existing = set(db.scalars(select(Category.name)))
    visible = sorted(
        (name for name in db.scalars(select(Category.name).where(Category.hidden.is_(False)))),
        key=str.casefold,
    )
    return RentalView(
        properties=properties,
        account_choices=[
            AccountChoice(account_id=row.id, text=_account_text(row)) for row in open_accounts
        ],
        categories=[CategoryChip(name=name, exists=name in existing) for name in chosen],
        category_choices=[name for name in visible if name not in chosen],
        message=message,
        error=error,
        saved=saved,
    )


def _account_text(account: Account | None) -> str:
    if account is None:
        return "(missing)"
    last4 = f" {last4_text(account.last4, account.alias_last4)}" if account.last4 else ""
    closed = " (closed)" if account.closed else ""
    return f"{account.name}{last4}{closed}"
