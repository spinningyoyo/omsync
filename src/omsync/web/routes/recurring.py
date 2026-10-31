"""The Recurring page: the Overview, the calendar, and All bills, the review drawer, the bill
drawer, its edit and add forms, and the writes. Each write is one batch from omsync.plan.recurring,
with a key the server made when it rendered the control. Its reply carries an undo toast whose Undo
posts to /batches/{id}/undo (routes/transactions.py, which calls undo_reply here). A successful
write sends the recurring-changed event, so the regions on the page reload themselves, and
close-dialogs where a dialog should close. A refused write answers 4xx with its message, shown where
the control was.

A possible bill has no ID, so Add bill and Not recurring post its identity: the payee pattern,
account, cadence, and typical amount. The server finds the suggestion again with detect, and
answers 409 when it has changed since the page was drawn. A decision in the review drawer answers
with the suggestion now at the same place in the queue, since the one decided has left it.

A write from Needs attention answers with the toast. A write from the bill drawer carries
from_drawer, and its reply shows the drawer as it now is.
"""

import json
from datetime import date
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError, StalePreview, applied_batch_for_key
from omsync.core.money import format_cents, to_cents
from omsync.core.months import parse_day
from omsync.ledger.categories_admin import category_choices
from omsync.ledger.models import Account, Category, Transaction
from omsync.ledger.plan_models import Cadence, RecurringSeries
from omsync.plan.recurring import (
    SeriesSuggestion,
    accept_new_amount,
    add_series,
    amount_mode,
    confirm,
    current_amount,
    delete_series,
    detect,
    dismiss,
    edited_amount,
    end_series,
    fixed_tolerance_cents,
    mark_paid_elsewhere,
    observed_tolerance_cents,
    payee_pattern_for,
    price_history,
    reject_charge,
    restore_dismissed,
    resume_series,
    update_series,
)
from omsync.web.deps import get_db
from omsync.web.recurring_view import (
    account_choices,
    build_body,
    build_drawer,
    noun_for,
    plain_dollars,
    review_view,
    viewed_month,
)
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/recurring.html"
FRAGMENTS = "recurring/fragments.html"
CHANGED = "recurring-changed"
CLOSE = "close-dialogs"
VIEWS = ("overview", "calendar", "all")
STATUSES = ("active", "ended", "dismissed")
STALE_SUGGESTION = "This suggestion changed. Reload the page to see the current suggestions."


def _today() -> date:
    return date.today()


def undo_reply(request: Request, db: Session) -> HTMLResponse:
    """The reply to undoing a recurring batch: the toast, and the regions reload."""
    return _saved(request, None, "Undone")


# Page and regions


@router.get("/recurring", response_class=HTMLResponse)
def recurring_page(
    request: Request,
    db: DbSession,
    view: str = "overview",
    month: str | None = None,
    status: str = "active",
    add_from: str | None = None,
) -> Response:
    # The retired History view is All bills with the dismissed filter, and the URL says so.
    if view == "history":
        return RedirectResponse("/recurring?view=all&status=dismissed", status_code=303)
    today = _today()
    context = shell_context(request, db, active="recurring", title="Recurring")
    return request.app.state.templates.TemplateResponse(
        request,
        PAGE,
        {
            **context,
            "body": build_body(
                db,
                today,
                _shown_view(view),
                viewed_month(month, today),
                _shown_status(status),
            ),
            "add_from": _transaction_id(add_from),
        },
    )


@router.get("/recurring/body", response_class=HTMLResponse)
def body_region(
    request: Request,
    db: DbSession,
    view: str = "overview",
    month: str | None = None,
    status: str = "active",
) -> HTMLResponse:
    today = _today()
    return _block(
        request,
        "frag_body",
        body=build_body(
            db,
            today,
            _shown_view(view),
            viewed_month(month, today),
            _shown_status(status),
        ),
    )


# Review


@router.get("/recurring/review", response_class=HTMLResponse)
def review_drawer(
    request: Request, db: DbSession, index: Annotated[int, Query(ge=0)] = 0
) -> HTMLResponse:
    return _review_reply(request, db, index)


@router.post("/recurring/suggestions/confirm", response_class=HTMLResponse)
def confirm_suggestion(
    request: Request,
    db: DbSession,
    payee_pattern: Annotated[str, Form()],
    account_id: Annotated[int, Form()],
    cadence: Annotated[str, Form()],
    amount_cents: Annotated[int, Form()],
    index: Annotated[int, Form()] = 0,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    replayed = _replay(request, db, idempotency_key, index)
    if replayed is not None:
        return replayed
    suggestion = _find_suggestion(db, _today(), payee_pattern, account_id, cadence, amount_cents)
    if suggestion is None:
        return _review_reply(request, db, index, status=409, error=STALE_SUGGESTION)
    try:
        batch = confirm(db, suggestion, idempotency_key=idempotency_key or uuid4().hex)
    except (ValueError, BatchError) as exc:
        return _review_reply(request, db, index, status=_refused(exc), error=str(exc))
    return _review_saved(request, db, index, batch, f"Added {suggestion.name}")


@router.post("/recurring/suggestions/dismiss", response_class=HTMLResponse)
def dismiss_suggestion(
    request: Request,
    db: DbSession,
    payee_pattern: Annotated[str, Form()],
    account_id: Annotated[int, Form()],
    cadence: Annotated[str, Form()],
    amount_cents: Annotated[int, Form()],
    index: Annotated[int, Form()] = 0,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    replayed = _replay(request, db, idempotency_key, index)
    if replayed is not None:
        return replayed
    suggestion = _find_suggestion(db, _today(), payee_pattern, account_id, cadence, amount_cents)
    if suggestion is None:
        return _review_reply(request, db, index, status=409, error=STALE_SUGGESTION)
    try:
        batch = dismiss(db, suggestion, idempotency_key=idempotency_key or uuid4().hex)
    except (ValueError, BatchError) as exc:
        return _review_reply(request, db, index, status=_refused(exc), error=str(exc))
    return _review_saved(request, db, index, batch, f"Dismissed {suggestion.name}")


# Bills: add

# The add form takes its fields as typed. A new bill is confirmed at once.


@router.get("/recurring/bills/new", response_class=HTMLResponse)
def add_form(
    request: Request,
    db: DbSession,
    from_txn: Annotated[int | None, Query(alias="from")] = None,
) -> HTMLResponse:
    return _block(
        request,
        "frag_add_form",
        form=_add_form(db, key=uuid4().hex, **_prefill(db, from_txn)),
    )


@router.post("/recurring/bills", response_class=HTMLResponse)
def add_bill(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form()] = "",
    payee: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    cadence: Annotated[str, Form()] = "monthly",
    amount: Annotated[str, Form()] = "",
    income: Annotated[str, Form()] = "",
    starts_on: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    typed = {
        "name": name,
        "payee": payee,
        "account_id": account_id,
        "cadence": cadence,
        "amount": amount,
        "income": income,
        "starts_on": starts_on,
        "category_id": category_id,
    }
    try:
        values = _add_values(db, **typed)
    except ValueError as exc:
        return _add_reply(request, db, key, typed, status=422, error=str(exc))
    try:
        batch = add_series(db, idempotency_key=key, **values)
    except (ValueError, BatchError) as exc:
        return _add_reply(request, db, key, typed, status=_refused(exc), error=str(exc))
    return _saved(request, batch, f"Added {values['name']}", close=True)


# Series: the drawer and edit form


@router.get("/recurring/series/{series_id:int}/drawer", response_class=HTMLResponse)
def series_drawer(
    request: Request, db: DbSession, series_id: int, due: str | None = None
) -> HTMLResponse:
    series = _series_or_404(db, series_id)
    return _block(
        request,
        "frag_drawer",
        drawer=build_drawer(db, series, _today(), _day(due)),
    )


@router.get("/recurring/series/{series_id:int}/edit", response_class=HTMLResponse)
def series_edit(request: Request, db: DbSession, series_id: int) -> HTMLResponse:
    series = _series_or_404(db, series_id)
    return _block(request, "frag_series_edit", form=_edit_form(db, series, key=uuid4().hex))


@router.post("/recurring/series/{series_id:int}", response_class=HTMLResponse)
def save_series(
    request: Request,
    db: DbSession,
    series_id: int,
    name: Annotated[str, Form()] = "",
    cadence: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    mode: Annotated[str, Form(alias="amount_mode")] = "",
    tolerance: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    series = _series_or_404(db, series_id)
    key = idempotency_key or uuid4().hex
    typed = {
        "name": name,
        "cadence": cadence,
        "amount": amount,
        "mode": mode,
        "tolerance": tolerance,
        "category_id": category_id,
    }
    try:
        values = _edit_values(db, series, **typed)
    except ValueError as exc:
        return _block(
            request,
            "frag_series_edit",
            status=422,
            form=_edit_form(db, series, key=key, error=str(exc), **typed),
        )
    if not _changes(series, values):
        return _drawer_saved(request, db, series, "Nothing changed", None)
    try:
        batch = update_series(db, series_id, idempotency_key=key, **values)
    except (ValueError, BatchError) as exc:
        return _block(
            request,
            "frag_series_edit",
            status=_refused(exc),
            form=_edit_form(db, series, key=key, error=str(exc), **typed),
        )
    return _drawer_saved(request, db, series, f"Saved {values['name']}", batch)


@router.post("/recurring/series/{series_id:int}/delete", response_class=HTMLResponse)
def delete_series_route(
    request: Request,
    db: DbSession,
    series_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    # No lookup first: a repeated key must return its batch even though the row is gone now.
    name = _name_of(db, series_id)
    try:
        batch = delete_series(db, series_id, idempotency_key=idempotency_key or uuid4().hex)
    except (ValueError, BatchError) as exc:
        return _saved_refusal(request, str(exc), _refused(exc))
    return _saved(request, batch, f"Deleted {name}", close=True)


@router.post("/recurring/series/{series_id:int}/restore", response_class=HTMLResponse)
def restore_series(
    request: Request,
    db: DbSession,
    series_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    name = _name_of(db, series_id)
    try:
        batch = restore_dismissed(db, series_id, idempotency_key=idempotency_key or uuid4().hex)
    except (ValueError, BatchError) as exc:
        return _saved_refusal(request, str(exc), _refused(exc))
    return _saved(request, batch, f"Restored {name}")


# Series: the questions in Needs attention and the drawer's ends and marks. Each one is one batch.


@router.post("/recurring/series/{series_id:int}/end", response_class=HTMLResponse)
def end_bill(
    request: Request,
    db: DbSession,
    series_id: int,
    ended_on: Annotated[str, Form()] = "",
    from_drawer: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    name = _name_of(db, series_id)
    try:
        batch = end_series(
            db,
            series_id,
            ended_on=_date(ended_on, "Pick the last day the bill is due."),
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except (ValueError, BatchError) as exc:
        return _refused_reply(request, db, series_id, str(exc), _refused(exc), from_drawer)
    return _written(request, db, series_id, batch, f"Ended {name}", from_drawer)


@router.post("/recurring/series/{series_id:int}/resume", response_class=HTMLResponse)
def resume_bill(
    request: Request,
    db: DbSession,
    series_id: int,
    from_drawer: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    name = _name_of(db, series_id)
    try:
        batch = resume_series(db, series_id, idempotency_key=idempotency_key or uuid4().hex)
    except (ValueError, BatchError) as exc:
        return _refused_reply(request, db, series_id, str(exc), _refused(exc), from_drawer)
    return _written(request, db, series_id, batch, f"Resumed {name}", from_drawer)


@router.post("/recurring/series/{series_id:int}/paid-elsewhere", response_class=HTMLResponse)
def mark_elsewhere(
    request: Request,
    db: DbSession,
    series_id: int,
    due_on: Annotated[str, Form()] = "",
    from_drawer: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    name = _name_of(db, series_id)
    try:
        due = _date(due_on, "Pick the due date to mark.")
        batch = mark_paid_elsewhere(
            db, series_id, due, idempotency_key=idempotency_key or uuid4().hex
        )
    except (ValueError, BatchError) as exc:
        return _refused_reply(request, db, series_id, str(exc), _refused(exc), from_drawer)
    return _written(
        request, db, series_id, batch, f"Marked {name} paid elsewhere", from_drawer, due=due
    )


@router.post("/recurring/series/{series_id:int}/accept-amount", response_class=HTMLResponse)
def accept_amount(
    request: Request,
    db: DbSession,
    series_id: int,
    transaction_id: Annotated[str, Form()] = "",
    from_drawer: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    name = _name_of(db, series_id)
    try:
        batch = accept_new_amount(
            db,
            series_id,
            _charge_id(transaction_id),
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except (ValueError, BatchError) as exc:
        return _refused_reply(request, db, series_id, str(exc), _refused(exc), from_drawer)
    return _written(request, db, series_id, batch, f"Used the new amount for {name}", from_drawer)


@router.post("/recurring/series/{series_id:int}/reject", response_class=HTMLResponse)
def reject_bill_charge(
    request: Request,
    db: DbSession,
    series_id: int,
    transaction_id: Annotated[str, Form()] = "",
    from_drawer: Annotated[bool, Form()] = False,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    name = _name_of(db, series_id)
    try:
        batch = reject_charge(
            db,
            series_id,
            _charge_id(transaction_id),
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except (ValueError, BatchError) as exc:
        return _refused_reply(request, db, series_id, str(exc), _refused(exc), from_drawer)
    return _written(request, db, series_id, batch, f"Marked not this bill for {name}", from_drawer)


# Helpers: lookups and validation


def _shown_view(view: str) -> str:
    """The view a request names. Anything unknown, including the retired Upcoming view, is the
    Overview."""
    return view if view in VIEWS else "overview"


def _shown_status(status: str) -> str:
    return status if status in STATUSES else "active"


def _transaction_id(text: str | None) -> int | None:
    """A transaction ID named in a URL, or None when it is missing or not a whole number."""
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _find_suggestion(
    db: Session,
    today: date,
    payee_pattern: str,
    account_id: int,
    cadence: str,
    amount_cents: int,
) -> SeriesSuggestion | None:
    for suggestion in detect(db, today=today):
        if (
            suggestion.payee_pattern == payee_pattern
            and suggestion.account_id == account_id
            and suggestion.cadence.value == cadence
            and suggestion.amount_cents == amount_cents
        ):
            return suggestion
    return None


def _series_or_404(db: Session, series_id: int) -> RecurringSeries:
    row = db.get(RecurringSeries, series_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such series")
    return row


def _name_of(db: Session, series_id: int) -> str:
    row = db.get(RecurringSeries, series_id)
    return row.name if row is not None else "bill"


def _day(text: str | None) -> date | None:
    try:
        return parse_day(text) if text else None
    except ValueError:
        return None


def _date(text: str, message: str) -> date:
    try:
        return parse_day(text)
    except ValueError:
        raise ValueError(message) from None


def _charge_id(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise ValueError("That charge is not in the list.") from None


def _name(text: str) -> str:
    clean = text.strip()
    if not clean:
        raise ValueError("Give it a name.")
    return clean


def _payee(text: str) -> str:
    """The payee pattern a typed payee makes, the one a bill started from a transaction gets."""
    try:
        return payee_pattern_for(text)
    except ValueError:
        raise ValueError("Enter the payee as it appears on statements.") from None


def _tolerance(text: str) -> int:
    try:
        cents = to_cents(text.strip())
    except (ValueError, TypeError):
        raise ValueError("Enter the exact tolerance in dollars, such as 1.00.") from None
    if cents < 0:
        raise ValueError("The tolerance cannot be negative.")
    return cents


def _money(text: str) -> int:
    try:
        cents = to_cents(text.strip())
    except (ValueError, TypeError):
        raise ValueError("Enter the amount in dollars, such as 45.99.") from None
    if cents <= 0:
        raise ValueError("The amount must be more than zero.")
    return cents


def _mode(text: str) -> str:
    if text not in ("fixed", "varies"):
        raise ValueError("Pick Fixed or Varies for the amount.")
    return text


def _category(db: Session, text: str) -> int | None:
    if not text.strip():
        return None
    try:
        category_id = int(text)
    except ValueError:
        raise ValueError("Pick a category from the list.") from None
    if db.get(Category, category_id) is None:
        raise ValueError("Pick a category from the list.")
    return category_id


def _account(db: Session, text: str) -> int | None:
    """None for Any account; otherwise the ID of an account that exists."""
    if not text.strip():
        return None
    try:
        account_id = int(text)
    except ValueError:
        raise ValueError("Pick an account from the list.") from None
    if db.get(Account, account_id) is None:
        raise ValueError("Pick an account from the list.")
    return account_id


def _cadence(text: str) -> Cadence:
    try:
        return Cadence(text)
    except ValueError:
        raise ValueError("Pick a cadence from the list.") from None


def _has_prices(db: Session, series: RecurringSeries) -> bool:
    """Whether the bill has accepted new amounts. Its amount then comes from them, not the form."""
    return len(price_history(db, series)) > 1


def _add_values(
    db: Session,
    *,
    name: str,
    payee: str,
    account_id: str,
    cadence: str,
    amount: str,
    income: str,
    starts_on: str,
    category_id: str,
) -> dict[str, Any]:
    magnitude = _money(amount)
    return {
        "name": _name(name),
        "payee_pattern": _payee(payee),
        "account_id": _account(db, account_id),
        "cadence": _cadence(cadence),
        "amount_cents": magnitude if income else -magnitude,
        "starts_on": _date(starts_on, "Pick the first due date."),
        "category_id": _category(db, category_id),
    }


def _edit_values(
    db: Session,
    series: RecurringSeries,
    *,
    name: str,
    cadence: str,
    amount: str,
    mode: str,
    tolerance: str,
    category_id: str,
) -> dict[str, Any]:
    """The edit form's values as update_series takes them. A bill with accepted new amounts keeps
    its amount, so no amount is posted (None). The tolerance is the exact one when Advanced is
    filled, else the Fixed or Varies one. Fixed is measured from the amount in effect after the
    save."""
    amount_cents = None if _has_prices(db, series) else edited_amount(series, _money(amount))
    exact = _tolerance(tolerance) if tolerance.strip() else None
    if exact is not None:
        tolerance_cents = exact
    elif _mode(mode) == "fixed":
        effective = current_amount(db, series) if amount_cents is None else amount_cents
        tolerance_cents = fixed_tolerance_cents(effective)
    else:
        tolerance_cents = observed_tolerance_cents(db, series)
    return {
        "name": _name(name),
        "cadence": _cadence(cadence),
        "amount_cents": amount_cents,
        "amount_tolerance_cents": tolerance_cents,
        "category_id": _category(db, category_id),
    }


def _changes(series: RecurringSeries, values: dict[str, Any]) -> bool:
    return any(
        [
            values["name"] != series.name,
            values["cadence"] != series.cadence,
            values["amount_cents"] is not None and values["amount_cents"] != series.amount_cents,
            values["amount_tolerance_cents"] != series.amount_tolerance_cents,
            values["category_id"] != series.category_id,
        ]
    )


def _edit_form(
    db: Session,
    series: RecurringSeries,
    *,
    key: str,
    name: str | None = None,
    cadence: str | None = None,
    amount: str | None = None,
    mode: str | None = None,
    tolerance: str | None = None,
    category_id: str | None = None,
    error: str = "",
) -> dict[str, Any]:
    current_category = "" if series.category_id is None else str(series.category_id)
    return {
        "series_id": series.id,
        "noun": noun_for(series.amount_cents),
        "name": series.name if name is None else name,
        "cadence": series.cadence.value if cadence is None else cadence,
        "has_prices": _has_prices(db, series),
        "current": format_cents(current_amount(db, series)),
        "amount": plain_dollars(series.amount_cents) if amount is None else amount,
        "mode": amount_mode(series) if mode is None else mode,
        "tolerance": "" if tolerance is None else tolerance,
        "tolerance_text": format_cents(series.amount_tolerance_cents),
        "category_id": current_category if category_id is None else category_id,
        "error": error,
        "key": key,
        "choices": category_choices(db),
    }


def _add_form(
    db: Session,
    *,
    key: str,
    name: str = "",
    payee: str = "",
    account_id: str = "",
    cadence: str = "monthly",
    amount: str = "",
    income: str = "",
    starts_on: str = "",
    category_id: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "payee": payee,
        "account_id": account_id,
        "cadence": cadence,
        "amount": amount,
        "income": income,
        "starts_on": starts_on or _today().isoformat(),
        "category_id": category_id,
        "error": error,
        "key": key,
        "accounts": account_choices(db),
        "choices": category_choices(db),
    }


def _prefill(db: Session, transaction_id: int | None) -> dict[str, str]:
    """The add form's fields for a transaction: its payee as the name and as the payee on
    statements, its account, its amount and sign, its posted day as the first due date, and its
    category. A missing transaction leaves the form blank."""
    txn = db.get(Transaction, transaction_id) if transaction_id is not None else None
    if txn is None:
        return {}
    return {
        "name": txn.payee,
        "payee": txn.payee_raw,
        "account_id": str(txn.account_id),
        "amount": plain_dollars(txn.amount_cents),
        "income": "1" if txn.amount_cents > 0 else "",
        "starts_on": txn.posted_on.isoformat(),
        "category_id": "" if txn.category_id is None else str(txn.category_id),
    }


# Helpers: replies


def _block(
    request: Request,
    block: str,
    *,
    status: int = 200,
    trigger: dict[str, bool] | None = None,
    **context: Any,
) -> HTMLResponse:
    response = request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, context, status_code=status, block_name=block
    )
    if trigger:
        response.headers["HX-Trigger"] = json.dumps(trigger)
    return response


def _review_reply(
    request: Request, db: Session, index: int, *, status: int = 200, error: str = ""
) -> HTMLResponse:
    """The review drawer at a place in the queue. A refusal shows its message above the card."""
    return _block(
        request,
        "frag_review",
        status=status,
        review=review_view(db, _today(), index),
        error=error,
    )


def _review_saved(
    request: Request, db: Session, index: int, batch: Batch, message: str
) -> HTMLResponse:
    """A decision that applied: the drawer shows the suggestion now at the same place, and the toast
    carries Undo."""
    return _block(
        request,
        "frag_review_saved",
        trigger={CHANGED: True},
        review=review_view(db, _today(), index),
        message=message,
        batch_id=batch.id,
    )


def _saved(
    request: Request, batch: Batch | None, message: str, *, close: bool = False
) -> HTMLResponse:
    """A write that applied: the regions reload, and the toast carries Undo for a batch."""
    trigger = {CHANGED: True, CLOSE: True} if close else {CHANGED: True}
    return _block(
        request,
        "frag_saved",
        trigger=trigger,
        message=message,
        batch_id=batch.id if batch is not None else None,
    )


def _saved_refusal(request: Request, message: str, status: int) -> HTMLResponse:
    """A write that did not apply, shown in the toast region."""
    return _block(request, "frag_saved", status=status, message=message, batch_id=None)


def _written(
    request: Request,
    db: Session,
    series_id: int,
    batch: Batch,
    message: str,
    from_drawer: bool,
    *,
    due: date | None = None,
) -> HTMLResponse:
    """A write from the drawer answers with the drawer as it now is, and one from Needs attention
    answers with the toast. The due date keeps the drawer on the occurrence it was opened from."""
    if not from_drawer:
        return _saved(request, batch, message)
    return _drawer_saved(request, db, _series_or_404(db, series_id), message, batch, due=due)


def _refused_reply(
    request: Request,
    db: Session,
    series_id: int,
    message: str,
    status: int,
    from_drawer: bool,
) -> HTMLResponse:
    """A refused write: the drawer is drawn again with the message, or the toast shows it."""
    if not from_drawer:
        return _saved_refusal(request, message, status)
    return _block(
        request,
        "frag_drawer_saved",
        status=status,
        drawer=build_drawer(db, _series_or_404(db, series_id), _today(), None),
        message=message,
        batch_id=None,
    )


def _add_reply(
    request: Request,
    db: Session,
    key: str,
    typed: dict[str, str],
    *,
    status: int,
    error: str,
) -> HTMLResponse:
    """The add form again, with the entered values kept and the message above them."""
    return _block(
        request,
        "frag_add_form",
        status=status,
        form=_add_form(db, key=key, error=error, **typed),
    )


VERBS = {
    "recurring_confirm": "Added",
    "recurring_dismiss": "Dismissed",
    "recurring_restore": "Restored",
    "recurring_update": "Saved",
    "recurring_delete": "Deleted",
}


def _replay(request: Request, db: Session, idempotency_key: str, index: int) -> HTMLResponse | None:
    """A key that already applied answers with its batch. A double click then shows the one change
    instead of a refusal for a suggestion that is no longer there."""
    if not idempotency_key:
        return None
    batch = applied_batch_for_key(db, idempotency_key)
    if batch is None:
        return None
    name = (batch.summary or {}).get("name", "bill")
    return _review_saved(request, db, index, batch, f"{VERBS.get(batch.kind, 'Saved')} {name}")


def _drawer_saved(
    request: Request,
    db: Session,
    series: RecurringSeries,
    message: str,
    batch: Batch | None,
    *,
    due: date | None = None,
) -> HTMLResponse:
    """A saved edit or drawer action: the drawer shows the bill as it now is, and the toast carries
    Undo."""
    db.refresh(series)
    return _block(
        request,
        "frag_drawer_saved",
        trigger={CHANGED: True},
        drawer=build_drawer(db, series, _today(), due),
        message=message,
        batch_id=batch.id if batch is not None else None,
    )


def _refused(exc: Exception) -> int:
    """409 when the data moved on since the page was drawn, 422 for anything the form can fix."""
    return 409 if isinstance(exc, StalePreview) else 422
