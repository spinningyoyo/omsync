"""Undo of any change, from the toast of the page that made it.

The undo route lives here because every page uses it. What redraws after an undo depends on the
kind of change and on the page the toast came from, so each page answers with its own reply.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch, BatchState
from omsync.core.batches import BatchError, UndoConflict
from omsync.reconcile.auto import undo_with_automatic
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.register_replies import FRAGMENTS as REGISTER_FRAGMENTS
from omsync.web.routes.accounts import (
    account_undo_reply,
    nicknames_undo_reply,
    numbers_undo_reply,
)
from omsync.web.routes.contacts import contacts_undo_reply
from omsync.web.routes.counterparty import transaction_undo_reply
from omsync.web.routes.plan import plan_undo_reply
from omsync.web.routes.reconcile import undo_reply as reconcile_undo_reply
from omsync.web.routes.recurring import undo_reply as recurring_undo_reply
from omsync.web.routes.settings_providers import provider_undo_reply
from omsync.web.routes.transactions import (
    register_refresh_undo_reply,
    review_undo_reply,
    row_undo_reply,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
UNDO_CONFLICT_MESSAGE = "Can't undo: changed since this was made."
UNDONE_WITH_AUTOMATIC = "Undone, with the automatic step that followed it"


@router.post("/batches/{batch_id}/undo", response_class=HTMLResponse)
def undo_batch(
    request: Request,
    db: DbSession,
    batch_id: int,
    account: int | None = None,
    f: str | None = None,
    saved: int | None = None,
    page: int = 1,
    start: str | None = None,
    end: str | None = None,
    plan: str | None = None,
    source: str | None = None,
    contact: int | None = None,
    txn: int | None = None,
) -> HTMLResponse:
    """Undo one batch and say so in the toast, together with the automatic batches that followed
    it (see undo_with_automatic). What re-renders depends on the batch: a register row
    is re-rendered out of band, a bulk edit or a saved filter re-renders the register, a review
    re-renders the review list, a reconcile or receipts batch re-runs the Reconcile results for the
    range in start and end, a contact batch re-renders the Contacts page, the transaction detail, or
    the Reconcile results, by the source the toast was sent from, a nickname batch re-renders
    the nicknames card of the account in account, and a number added on the account page (source
    "account") re-renders its numbers card, header, and the panel."""
    if db.get(Batch, batch_id) is None:
        raise HTTPException(status_code=404, detail="No such change")
    try:
        batch, automatic = undo_with_automatic(db, batch_id)
    except UndoConflict:
        return _toast(request, UNDO_CONFLICT_MESSAGE, status=409)
    except BatchError:
        current = db.get(Batch, batch_id)
        message = (
            "Can't undo: already undone."
            if current is not None and current.state is BatchState.undone
            else UNDO_CONFLICT_MESSAGE
        )
        return _toast(request, message, status=409)
    message = UNDONE_WITH_AUTOMATIC if automatic else "Undone"
    reply = _register_undo_reply(request, db, batch, message, account, f, saved, page, start, end)
    if reply is None:
        reply = _other_page_undo_reply(
            request, db, batch, message, account, plan, source, contact, txn, start, end
        )
    if reply is not None:
        return reply
    return row_undo_reply(request, db, batch, account)


def _register_undo_reply(
    request: Request,
    db: Session,
    batch: Batch,
    message: str,
    account: int | None,
    f: str | None,
    saved: int | None,
    page: int,
    start: str | None,
    end: str | None,
) -> HTMLResponse | None:
    """The reply for a batch made on the register, Categories, review, Reconcile, or the
    nicknames card; None for any other batch."""
    kind = batch.kind
    if _is_categories_batch(kind):
        return _categories_undone(request, message)
    if kind.startswith(("bulk.", "saved_filter.")):
        return register_refresh_undo_reply(request, db, account, f, saved, page)
    if kind == "review_resolve":
        return review_undo_reply(request, db)
    if kind.startswith("reconcile.") or kind == "receipts":
        return reconcile_undo_reply(request, db, start, end)
    if kind.startswith("account_nickname."):
        return nicknames_undo_reply(request, db, account, message)
    return None


def _other_page_undo_reply(
    request: Request,
    db: Session,
    batch: Batch,
    message: str,
    account: int | None,
    plan: str | None,
    source: str | None,
    contact: int | None,
    txn: int | None,
    start: str | None,
    end: str | None,
) -> HTMLResponse | None:
    """The reply for a batch made on the Contacts, providers, account, recurring, or plan pages;
    None for any other batch."""
    kind = batch.kind
    if kind.startswith("contact."):
        if source == "reconcile":
            return reconcile_undo_reply(request, db, start, end, automatic=True, message=message)
        if source == "transaction":
            return transaction_undo_reply(request, db, txn, message)
        return contacts_undo_reply(request, db, contact, message)
    if kind.startswith("provider_") or (kind.startswith("account.") and source == "providers"):
        return provider_undo_reply(request, db)
    account_id = (batch.summary or {}).get("account_id")
    account_id = account_id if isinstance(account_id, int) else None
    if kind.startswith("account."):
        return account_undo_reply(request, db, account_id)
    if kind == "add_alias" and source == "account":
        return numbers_undo_reply(request, db, account_id)
    if kind.startswith("recurring_"):
        return recurring_undo_reply(request, db)
    if kind.startswith(("budget_", "goal.")):
        return plan_undo_reply(request, db, plan)
    return None


def _is_categories_batch(kind: str) -> bool:
    """Batches made on the Categories page, and categorize, whose undo changes what it shows."""
    return kind in ("categorize", "keyword_import") or kind.startswith(
        ("category.", "keyword.", "rule.")
    )


def _categories_undone(request: Request, message: str) -> HTMLResponse:
    """The Categories page reloads the regions that show categories, keywords, and rules."""
    return render_block(
        request,
        REGISTER_FRAGMENTS,
        "frag_toast",
        trigger={"categories-changed": True},
        message=message,
    )


def _toast(request: Request, message: str, *, status: int = 200) -> HTMLResponse:
    return render_block(request, REGISTER_FRAGMENTS, "frag_toast", status=status, message=message)
