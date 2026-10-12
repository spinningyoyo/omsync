"""Self-name settings and matching-text preview."""

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.ledger.sources import Account, account_digits
from omsync.ledger.transactions import Transaction
from omsync.reconcile.self_names import get_self_names, set_self_names
from omsync.web.deps import get_db
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/settings_names.html"
FRAGMENTS = "settings/names_fragments.html"
PATH = "/settings/names"
PREVIEW_LIMIT = 20


@router.get("/settings/names", response_class=HTMLResponse)
def names_page(request: Request, db: DbSession) -> HTMLResponse:
    context = shell_context(request, db, active="", title="Your names")
    context.update(
        nav=SETTINGS_NAV,
        active_href=PATH,
        page_title="Your names",
        **_region_context(db),
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.post("/settings/names/add", response_class=HTMLResponse)
def add_name(request: Request, db: DbSession, name: Annotated[str, Form()] = "") -> HTMLResponse:
    names = get_self_names(db)
    try:
        set_self_names(db, [*names, name])
    except ValueError as exc:
        return _region(request, db, message=str(exc), status=422)
    return _region(request, db, message="Name saved.")


@router.post("/settings/names/remove", response_class=HTMLResponse)
def remove_name(request: Request, db: DbSession, name: Annotated[str, Form()] = "") -> HTMLResponse:
    kept = [saved for saved in get_self_names(db) if saved.casefold() != name.strip().casefold()]
    set_self_names(db, kept)
    return _region(request, db, message="Name removed.")


def _region(request: Request, db: Session, *, message: str, status: int = 200) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        FRAGMENTS,
        _region_context(db, message=message),
        status_code=status,
        block_name="frag_names",
    )


def _region_context(db: Session, *, message: str = "") -> dict[str, Any]:
    names = get_self_names(db)
    return {
        "names": names,
        "preview": _preview(db, names),
        "message": message,
        "add_key": uuid4().hex,
    }


def _preview(db: Session, names: list[str]) -> list[dict[str, str]]:
    """The newest transactions whose payee text (raw or display) contains a saved name."""
    if not names:
        return []
    contains = [
        or_(
            Transaction.payee_raw.contains(name, autoescape=True),
            Transaction.payee.contains(name, autoescape=True),
        )
        for name in names
    ]
    rows = db.execute(
        select(Transaction, Account)
        .join(Account, Account.id == Transaction.account_id)
        .where(or_(*contains))
        .order_by(Transaction.posted_on.desc(), Transaction.id.desc())
        .limit(PREVIEW_LIMIT)
    ).all()
    return [
        {
            "on": txn.posted_on.isoformat(),
            "account": account_digits(account.last4, account.alias_last4) or "—",
            "payee": txn.payee_raw,
            "amount": format_cents(txn.amount_cents),
        }
        for txn, account in rows
    ]
