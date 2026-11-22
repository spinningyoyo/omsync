"""The account page and its edit: /accounts/{id}. An edit is one batch from ledger.accounts_view,
and its reply carries the undo toast. Undo itself is /batches/{id}/undo
(routes/transactions.py), which calls account_undo_reply here for an account batch.

The account's nicknames are added and removed on the page too, as batches from ledger.contacts,
with the automatic step after each (see omsync.web.automatic). Their card is re-rendered by the
reply, and undo calls nicknames_undo_reply here.

Another card's last four digits are added in the numbers card, as an alias batch from
ledger.accounts_view. The reply re-renders the card, and the header and the panel out of band,
because the digits they show can change; undo calls numbers_undo_reply here.

A refused edit answers 422 with its message, and a stale one 409, so the dialog shows the message
and keeps what was typed. A saved or undone change re-renders the header and the accounts panel out
of band (accounts/fragments.html).
"""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import CHANGED_ELSEWHERE, BatchError
from omsync.ingest.institutions import recent_syncs
from omsync.ledger.accounts_view import (
    AccountDetail,
    account_detail,
    account_numbers,
    add_account_number,
    update_account,
)
from omsync.ledger.contacts import (
    add_account_nickname,
    list_nicknames,
    remove_account_nickname,
)
from omsync.web.account_page import (
    chart_points,
    connector_label,
    nickname_context,
    sync_views,
)
from omsync.web.accounts_panel import (
    GROUPINGS,
    TYPE_LABELS,
    account_groups,
    save_panel_grouping,
)
from omsync.web.automatic import run_automatic, with_automatic
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/account.html"
MISSING = "pages/account_missing.html"
FRAGMENTS = "accounts/fragments.html"


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_page(
    request: Request, db: DbSession, account_id: int, closed: bool = False
) -> HTMLResponse:
    """The page for one account. A closed account is found only with ?closed=1."""
    try:
        detail = account_detail(db, account_id)
    except LookupError:
        return _missing(request, db)
    if detail.summary.closed and not closed:
        return _missing(request, db)
    summary = detail.summary
    context = shell_context(request, db, active="", title=summary.name)
    context.update(
        account_groups=account_groups(db, active_id=account_id),
        active_account_id=account_id,
        detail=detail,
        type_label=TYPE_LABELS[summary.type],
        form_key=uuid4().hex,
        transactions_href=f"/transactions?account={account_id}",
        chart_points=chart_points(detail.points),
        connector=connector_label(detail.connector_key, detail.connector_enabled),
        syncs=sync_views(recent_syncs(db, detail.institution_key)),
        **nickname_context(db, account_id),
        numbers=account_numbers(db, account_id),
        number_key=uuid4().hex,
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.post("/accounts/{account_id}", response_class=HTMLResponse)
def edit_account(
    request: Request,
    db: DbSession,
    account_id: int,
    name: Annotated[str, Form()] = "",
    joint: Annotated[str, Form()] = "",
    closed: Annotated[str, Form()] = "",
    last4: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Apply the edit form. The checkboxes post "1" when ticked and nothing when cleared, so the
    form always sends the whole state and an absent box means not joint or not closed. A blank
    last four leaves the label alone."""
    key = idempotency_key or uuid4().hex
    try:
        batch = update_account(
            db,
            account_id,
            name=name,
            joint=joint == "1",
            closed=closed == "1",
            last4=last4 or None,
            idempotency_key=key,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such account") from None
    except ValueError as exc:
        return _fragment(request, "frag_edit_error", status_code=422, message=str(exc))
    except BatchError:
        return _fragment(request, "frag_edit_error", status_code=409, message=CHANGED_ELSEWHERE)
    label = str((batch.summary or {}).get("label", "Change saved"))
    return _saved(request, db, account_id, label=label, batch_id=batch.id)


@router.post("/accounts/{account_id}/nicknames", response_class=HTMLResponse)
def add_nickname_route(
    request: Request,
    db: DbSession,
    account_id: int,
    nickname: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        batch = add_account_nickname(
            db, account_id, nickname=nickname, idempotency_key=idempotency_key or uuid4().hex
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such account") from None
    except (ValueError, BatchError) as exc:
        return _nicknames_fragment(request, db, account_id, error=str(exc), status_code=422)
    return _nicknames_saved(request, db, account_id, batch)


@router.post("/accounts/{account_id}/nicknames/{nickname_id}/remove", response_class=HTMLResponse)
def remove_nickname_route(
    request: Request,
    db: DbSession,
    account_id: int,
    nickname_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if all(row.id != nickname_id or row.account_id != account_id for row in list_nicknames(db)):
        raise HTTPException(status_code=404, detail="No such nickname")
    try:
        batch = remove_account_nickname(
            db, nickname_id, idempotency_key=idempotency_key or uuid4().hex
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such nickname") from None
    except BatchError as exc:
        return _nicknames_fragment(request, db, account_id, error=str(exc), status_code=422)
    return _nicknames_saved(request, db, account_id, batch)


@router.post("/accounts/{account_id}/numbers", response_class=HTMLResponse)
def add_number_route(
    request: Request,
    db: DbSession,
    account_id: int,
    last4: Annotated[str, Form()] = "",
    receipts_only: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Add another card's last four digits: a reissued card's, which sync then looks for, or, with
    receipts_only, a card number only receipts show."""
    try:
        batch = add_account_number(
            db,
            account_id,
            last4,
            idempotency_key=idempotency_key or uuid4().hex,
            receipts_only=receipts_only,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such account") from None
    except (ValueError, BatchError) as exc:
        return _fragment(
            request,
            "frag_numbers",
            status_code=422,
            account_id=account_id,
            error=str(exc),
            numbers=account_numbers(db, account_id),
            number_key=uuid4().hex,
        )
    detail = account_detail(db, account_id)
    return _fragment(
        request,
        "frag_numbers",
        account_id=account_id,
        message=f"Added ••{last4.strip()}",
        batch_id=batch.id,
        numbers=account_numbers(db, account_id),
        number_key=uuid4().hex,
        **_header_and_panel(db, account_id, detail),
    )


@router.post("/accounts/panel/grouping/{grouping}", response_class=HTMLResponse)
def set_panel_grouping(
    request: Request, db: DbSession, grouping: str, active: int | None = None
) -> HTMLResponse:
    """Save how the accounts panel groups accounts, then answer with its grouping control and its
    groups out of band. active is the account whose page the panel is on, so its highlight stays."""
    if grouping not in GROUPINGS:
        raise HTTPException(status_code=404, detail="No such grouping")
    save_panel_grouping(db, grouping)
    return _fragment(
        request,
        "frag_grouping",
        grouping=grouping,
        active_account_id=active,
        groups=account_groups(db, active_id=active),
    )


def account_undo_reply(request: Request, db: Session, account_id: int | None) -> HTMLResponse:
    """The reply to undoing an account batch: the toast, then the header and the panel as they are
    now. An account that no longer exists gets the toast and the panel only."""
    detail = None
    if account_id is not None:
        try:
            detail = account_detail(db, account_id)
        except LookupError:
            detail = None
    return _fragment(
        request,
        "frag_undone",
        message="Undone",
        **_header_and_panel(db, account_id, detail),
    )


def nicknames_undo_reply(
    request: Request, db: Session, account_id: int | None, message: str
) -> HTMLResponse:
    """After an undo of a nickname batch: the toast, and the nicknames card out of band. The
    automatic step runs again, because what it identified rested on the nickname."""
    message = with_automatic(message, run_automatic(db))
    context = nickname_context(db, account_id) if account_id is not None else {}
    return _fragment(
        request,
        "frag_nicknames_undone",
        message=message,
        account_id=account_id,
        **context,
    )


def numbers_undo_reply(request: Request, db: Session, account_id: int | None) -> HTMLResponse:
    """After an undo of an added number: the toast, then the numbers card, the header, and the
    panel as they are now. An account that no longer exists gets the toast and the panel only."""
    detail = None
    if account_id is not None:
        try:
            detail = account_detail(db, account_id)
        except LookupError:
            detail = None
    return _fragment(
        request,
        "frag_numbers_undone",
        message="Undone",
        account_id=account_id,
        numbers=account_numbers(db, account_id) if detail is not None else (),
        number_key=uuid4().hex,
        **_header_and_panel(db, account_id, detail),
    )


def _nicknames_saved(request: Request, db: Session, account_id: int, batch: Batch) -> HTMLResponse:
    """The card after a saved nickname change, with the automatic step run and its toast."""
    label = str((batch.summary or {}).get("label", "Change saved"))
    message = with_automatic(label, run_automatic(db, batch.id))
    return _nicknames_fragment(request, db, account_id, message=message, batch_id=batch.id)


def _nicknames_fragment(
    request: Request,
    db: Session,
    account_id: int,
    *,
    message: str = "",
    batch_id: int | None = None,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return _fragment(
        request,
        "frag_nicknames",
        status_code=status_code,
        account_id=account_id,
        message=message,
        batch_id=batch_id,
        error=error,
        **nickname_context(db, account_id),
    )


def _saved(
    request: Request, db: Session, account_id: int, *, label: str, batch_id: int
) -> HTMLResponse:
    detail = account_detail(db, account_id)
    return _fragment(
        request,
        "frag_saved",
        label=label,
        batch_id=batch_id,
        **_header_and_panel(db, account_id, detail),
    )


def _header_and_panel(
    db: Session, account_id: int | None, detail: AccountDetail | None
) -> dict[str, object]:
    """The values the header (when there is an account) and the accounts panel are drawn from.
    Each reply gets a fresh idempotency key, so the edit form it renders is ready for a new save."""
    parts: dict[str, object] = {
        "groups": account_groups(db, active_id=account_id),
        "detail": detail,
        "form_key": uuid4().hex,
        "type_label": "",
        "transactions_href": "",
    }
    if detail is not None:
        parts["type_label"] = TYPE_LABELS[detail.summary.type]
        parts["transactions_href"] = f"/transactions?account={detail.summary.id}"
    return parts


def _missing(request: Request, db: Session) -> HTMLResponse:
    context = shell_context(request, db, active="", title="Account not found")
    return request.app.state.templates.TemplateResponse(request, MISSING, context, status_code=404)


def _fragment(
    request: Request, block: str, *, status_code: int = 200, **context: object
) -> HTMLResponse:
    return render_block(request, FRAGMENTS, block, status=status_code, **context)
