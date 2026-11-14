"""Import-review page and resolution responses."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError, StalePreview
from omsync.ingest.importer import resolve_review
from omsync.ledger.accounts import AccountError
from omsync.ledger.models import Transaction
from omsync.ledger.register import open_review_count, open_reviews
from omsync.web.deps import get_db
from omsync.web.register_replies import RegisterError, fragment, review_items
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/transactions.html"
STALE = "This changed elsewhere, reload."


@router.get("/transactions/review", response_class=HTMLResponse)
def review_page(request: Request, db: DbSession) -> HTMLResponse:
    context = shell_context(request, db, active="transactions", title="Transactions")
    return request.app.state.templates.TemplateResponse(
        request,
        PAGE,
        {
            **context,
            "view": "review",
            "account": None,
            "all_href": "/transactions",
            "review_count": open_review_count(db),
            "items": review_items(open_reviews(db)),
            "message": "",
        },
    )


@router.post("/transactions/review/{review_id}/resolve", response_class=HTMLResponse)
def review_resolve(
    request: Request,
    db: DbSession,
    review_id: int,
    transaction_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Settle one review: the row joins the transaction chosen, or becomes a new one."""
    chosen = _chosen(transaction_id)
    try:
        batch = resolve_review(
            db,
            review_id,
            transaction_id=chosen,
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except StalePreview:
        return _list(request, db, status_code=409, message=STALE)
    except BatchError as exc:
        return _list(request, db, status_code=409, message=str(exc))
    except AccountError as exc:
        return _list(request, db, status_code=422, message=str(exc))
    label = "Added as a new transaction"
    if chosen is not None:
        target = db.get(Transaction, chosen)
        if target is not None:
            label = f"Matched to {target.payee} on {target.posted_on.isoformat()}"
    return fragment(
        request,
        "frag_review_resolved",
        items=review_items(open_reviews(db)),
        review_count=open_review_count(db),
        label=label,
        batch_id=batch.id,
        message="",
    )


def _chosen(text: str) -> int | None:
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        raise RegisterError("That transaction does not exist.", 422) from None


def _list(request: Request, db: Session, *, status_code: int, message: str) -> HTMLResponse:
    """The review list again, with the reason the action did not happen."""
    return fragment(
        request,
        "frag_review_list",
        status_code=status_code,
        items=review_items(open_reviews(db)),
        message=message,
        autofocus=False,
    )
