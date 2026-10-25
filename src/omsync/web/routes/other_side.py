"""A transaction's detail page, and setting the other side of it or of one of its splits.

The detail page shows who is on the other side of the money, and whether omsync recognized it or
you set it. Setting it is one batch (omsync.ledger.other_side), followed in the same request by the
automatic step. A split with no other side of its own belongs to its transaction's contact.
"""

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.ledger.other_side import set_other_side, set_split_other_side
from omsync.web.automatic import run_automatic, with_automatic
from omsync.web.contacts_view import parse_choice
from omsync.web.deps import get_db
from omsync.web.other_side_view import TransactionDetail, load_detail
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/transaction.html"
FRAGMENTS = "transactions/detail_fragments.html"


@router.get("/transactions/{txn_id}/detail", response_class=HTMLResponse)
def detail_page(request: Request, db: DbSession, txn_id: int) -> HTMLResponse:
    context = shell_context(request, db, active="transactions", title="Transaction")
    context.update(detail=_detail_or_404(db, txn_id))
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.post("/transactions/{txn_id}/other-side", response_class=HTMLResponse)
def set_route(
    request: Request,
    db: DbSession,
    txn_id: int,
    target: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Set the other side to the contact (and account) in target, or clear it when empty."""
    _detail_or_404(db, txn_id)
    try:
        contact_id, account_id = parse_choice(target) if target.strip() else (None, None)
        batch = set_other_side(
            db,
            transaction_ids=[txn_id],
            contact_id=contact_id,
            contact_account_id=account_id,
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except (LookupError, ValueError, BatchError) as exc:
        return _error(request, db, txn_id, str(exc))
    return _saved(request, db, txn_id, batch.id, _label(batch))


@router.post("/transactions/{txn_id}/splits/{split_id}/other-side", response_class=HTMLResponse)
def set_split_route(
    request: Request,
    db: DbSession,
    txn_id: int,
    split_id: int,
    target: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """The same for one split of the transaction."""
    detail = _detail_or_404(db, txn_id)
    if split_id not in {part.split.id for part in detail.splits}:
        raise HTTPException(status_code=404, detail="No such split")
    try:
        contact_id, account_id = parse_choice(target) if target.strip() else (None, None)
        batch = set_split_other_side(
            db,
            split_id,
            contact_id=contact_id,
            contact_account_id=account_id,
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except (LookupError, ValueError, BatchError) as exc:
        return _error(request, db, txn_id, str(exc))
    return _saved(request, db, txn_id, batch.id, _label(batch))


def transaction_undo_reply(
    request: Request, db: Session, txn_id: int | None, message: str = "Undone"
) -> HTMLResponse:
    """After an undo made on the detail page: the toast, and the page's region out of band. The
    automatic step runs again, because what it identified rested on what was undone."""
    message = with_automatic(message, run_automatic(db))
    try:
        detail = load_detail(db, txn_id) if txn_id is not None else None
    except LookupError:
        detail = None
    return _fragment(request, "frag_undo_reply", message=message, txn_id=txn_id, detail=detail)


def _saved(request: Request, db: Session, txn_id: int, batch_id: int, label: str) -> HTMLResponse:
    message = with_automatic(label, run_automatic(db, batch_id))
    return _fragment(
        request,
        "frag_saved",
        message=message,
        batch_id=batch_id,
        txn_id=txn_id,
        detail=load_detail(db, txn_id),
    )


def _error(request: Request, db: Session, txn_id: int, message: str) -> HTMLResponse:
    return _fragment(
        request,
        "frag_error",
        status=422,
        error=message,
        txn_id=txn_id,
        detail=load_detail(db, txn_id),
    )


def _detail_or_404(db: Session, txn_id: int) -> TransactionDetail:
    try:
        return load_detail(db, txn_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such transaction") from None


def _label(batch: Any) -> str:
    return str((batch.summary or {}).get("label", "Change saved"))


def _fragment(request: Request, block: str, *, status: int = 200, **context: Any) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, context, status_code=status, block_name=block
    )
