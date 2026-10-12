"""The Recurring page: the four views, the suggestions panel, the series drawer and its edit form,
and the writes. Each write is one batch from omsync.plan.recurring, with a key the server made
when it rendered the control. Its reply carries an undo toast whose Undo posts to /batches/{id}/undo
(routes/transactions.py, which calls undo_reply here). A successful write sends the
recurring-changed event, so the regions on the page reload themselves, and close-dialogs where a
dialog should close. A refused write answers 4xx with its message.

A suggestion has no ID, so confirm and dismiss post its identity: the payee pattern, account,
cadence, and typical amount. The server finds the suggestion again with detect, and answers 409 when
it has changed since the page was drawn.
"""

import json
from dataclasses import replace
from datetime import date
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError, StalePreview, applied_batch_for_key
from omsync.core.money import format_cents, to_cents
from omsync.ledger.categories_admin import category_choices
from omsync.ledger.models import Category
from omsync.ledger.plan_models import Cadence, RecurringSeries, SeriesStatus
from omsync.plan.recurring import (
    SeriesSuggestion,
    confirm,
    delete_series,
    detect,
    dismiss,
    edited_amount,
    refresh,
    restore_dismissed,
    update_series,
)
from omsync.web.deps import get_db
from omsync.web.recurring_view import (
    CADENCE_LABELS,
    build_body,
    build_drawer,
    build_summary,
    plain_dollars,
    suggestion_rows,
    viewed_month,
)
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/recurring.html"
FRAGMENTS = "recurring/fragments.html"
CHANGED = "recurring-changed"
CLOSE = "close-dialogs"
VIEWS = ("upcoming", "calendar", "all", "history")
STALE_SUGGESTION = "This suggestion changed. Reload the page to see the current suggestions."


def _today() -> date:
    return date.today()


def undo_reply(request: Request, db: Session) -> HTMLResponse:
    """The reply to undoing a recurring batch: the toast, and the regions reload."""
    return _saved(request, None, "Undone")


# Page and regions


@router.get("/recurring", response_class=HTMLResponse)
def recurring_page(
    request: Request, db: DbSession, view: str = "upcoming", month: str | None = None
) -> HTMLResponse:
    today = _today()
    selected = view if view in VIEWS else "upcoming"
    context = shell_context(request, db, active="recurring", title="Recurring")
    return request.app.state.templates.TemplateResponse(
        request,
        PAGE,
        {
            **context,
            "view": selected,
            "summary": build_summary(db, today),
            "suggestions": suggestion_rows(db, today),
            "body": build_body(db, today, selected, viewed_month(month, today)),
        },
    )


@router.get("/recurring/body", response_class=HTMLResponse)
def body_region(
    request: Request, db: DbSession, view: str = "upcoming", month: str | None = None
) -> HTMLResponse:
    today = _today()
    selected = view if view in VIEWS else "upcoming"
    return _block(
        request,
        "frag_body",
        body=build_body(db, today, selected, viewed_month(month, today)),
    )


@router.get("/recurring/summary", response_class=HTMLResponse)
def summary_region(request: Request, db: DbSession) -> HTMLResponse:
    return _block(request, "frag_summary", summary=build_summary(db, _today()))


@router.get("/recurring/suggestions", response_class=HTMLResponse)
def suggestions_region(request: Request, db: DbSession) -> HTMLResponse:
    return _block(request, "frag_suggestions", suggestions=suggestion_rows(db, _today()))


# Suggestions


@router.get("/recurring/suggestions/confirm", response_class=HTMLResponse)
def confirm_form(
    request: Request,
    db: DbSession,
    payee_pattern: str,
    account_id: int,
    cadence: str,
    amount_cents: int,
) -> HTMLResponse:
    suggestion = _find_suggestion(db, _today(), payee_pattern, account_id, cadence, amount_cents)
    if suggestion is None:
        return _block(request, "frag_message", status=409, message=STALE_SUGGESTION)
    return _block(
        request,
        "frag_confirm_form",
        form=_confirm_form(db, suggestion, key=uuid4().hex),
    )


@router.post("/recurring/suggestions/confirm", response_class=HTMLResponse)
def confirm_suggestion(
    request: Request,
    db: DbSession,
    payee_pattern: Annotated[str, Form()],
    account_id: Annotated[int, Form()],
    cadence: Annotated[str, Form()],
    amount_cents: Annotated[int, Form()],
    name: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    tolerance: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    replayed = _replay(request, db, idempotency_key)
    if replayed is not None:
        return replayed
    suggestion = _find_suggestion(db, _today(), payee_pattern, account_id, cadence, amount_cents)
    if suggestion is None:
        return _block(request, "frag_message", status=409, message=STALE_SUGGESTION)
    key = idempotency_key or uuid4().hex
    try:
        clean_name = _name(name)
        category = _category(db, category_id)
        tolerance_cents = _tolerance(tolerance)
    except ValueError as exc:
        return _block(
            request,
            "frag_confirm_form",
            status=422,
            form=_confirm_form(
                db,
                suggestion,
                key=key,
                name=name,
                category_id=category_id,
                tolerance=tolerance,
                error=str(exc),
            ),
        )
    edited = replace(
        suggestion,
        name=clean_name,
        category_id=category,
        amount_tolerance_cents=tolerance_cents,
    )
    try:
        batch = confirm(db, edited, idempotency_key=key)
    except (ValueError, BatchError) as exc:
        return _block(
            request,
            "frag_confirm_form",
            status=_refused(exc),
            form=_confirm_form(db, suggestion, key=key, error=str(exc)),
        )
    return _saved(request, batch, f"Confirmed {clean_name}", close=True)


@router.post("/recurring/suggestions/dismiss", response_class=HTMLResponse)
def dismiss_suggestion(
    request: Request,
    db: DbSession,
    payee_pattern: Annotated[str, Form()],
    account_id: Annotated[int, Form()],
    cadence: Annotated[str, Form()],
    amount_cents: Annotated[int, Form()],
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    replayed = _replay(request, db, idempotency_key)
    if replayed is not None:
        return replayed
    suggestion = _find_suggestion(db, _today(), payee_pattern, account_id, cadence, amount_cents)
    if suggestion is None:
        return _saved_refusal(request, STALE_SUGGESTION, 409)
    try:
        batch = dismiss(db, suggestion, idempotency_key=idempotency_key or uuid4().hex)
    except (ValueError, BatchError) as exc:
        return _saved_refusal(request, str(exc), _refused(exc))
    return _saved(request, batch, f"Dismissed {suggestion.name}")


# Series


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


@router.post("/recurring/refresh", response_class=HTMLResponse)
def refresh_all(request: Request, db: DbSession) -> HTMLResponse:
    """Refresh every confirmed series. Each series is its own batch, and a series with nothing new
    writes nothing. One change can be undone from the toast; several cannot, so the toast says how
    many changed and offers no undo."""
    today = _today()
    ids = list(
        db.scalars(
            select(RecurringSeries.id)
            .where(RecurringSeries.status == SeriesStatus.confirmed)
            .order_by(RecurringSeries.id)
        )
    )
    changed: list[tuple[int, Batch]] = []
    try:
        for series_id in ids:
            batch = refresh(db, series_id, today=today)
            if batch is not None:
                changed.append((series_id, batch))
    except (ValueError, BatchError) as exc:
        return _saved_refusal(request, str(exc), _refused(exc))
    if not changed:
        return _saved(request, None, "Already up to date")
    if len(changed) == 1:
        series_id, batch = changed[0]
        return _saved(request, batch, f"Refreshed {_name_of(db, series_id)}")
    return _saved(request, None, f"Refreshed {len(changed)} series")


# Helpers: lookups and validation


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
    return row.name if row is not None else "series"


def _day(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _name(text: str) -> str:
    clean = text.strip()
    if not clean:
        raise ValueError("Give the series a name.")
    return clean


def _tolerance(text: str) -> int:
    try:
        cents = to_cents(text.strip() or "0")
    except (ValueError, TypeError):
        raise ValueError("Enter the tolerance in dollars, such as 1.00.") from None
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


def _cadence(text: str) -> Cadence:
    try:
        return Cadence(text)
    except ValueError:
        raise ValueError("Pick a cadence from the list.") from None


def _edit_values(
    db: Session,
    series: RecurringSeries,
    *,
    name: str,
    cadence: str,
    amount: str,
    tolerance: str,
    category_id: str,
) -> dict[str, Any]:
    magnitude = _money(amount)
    return {
        "name": _name(name),
        "cadence": _cadence(cadence),
        "amount_cents": edited_amount(series, magnitude),
        "amount_tolerance_cents": _tolerance(tolerance),
        "category_id": _category(db, category_id),
    }


def _changes(series: RecurringSeries, values: dict[str, Any]) -> bool:
    return any(
        [
            values["name"] != series.name,
            values["cadence"] != series.cadence,
            values["amount_cents"] != series.amount_cents,
            values["amount_tolerance_cents"] != series.amount_tolerance_cents,
            values["category_id"] != series.category_id,
        ]
    )


def _confirm_form(
    db: Session,
    suggestion: SeriesSuggestion,
    *,
    key: str,
    name: str | None = None,
    category_id: str | None = None,
    tolerance: str | None = None,
    error: str = "",
) -> dict[str, Any]:
    current_category = "" if suggestion.category_id is None else str(suggestion.category_id)
    summary = (
        f"{CADENCE_LABELS[suggestion.cadence.value]} · {format_cents(suggestion.amount_cents)}"
        f" ± {format_cents(suggestion.amount_tolerance_cents)}"
    )
    return {
        "summary": summary,
        "name": suggestion.name if name is None else name,
        "category_id": current_category if category_id is None else category_id,
        "tolerance": (
            plain_dollars(suggestion.amount_tolerance_cents) if tolerance is None else tolerance
        ),
        "error": error,
        "key": key,
        "identity": {
            "payee_pattern": suggestion.payee_pattern,
            "account_id": str(suggestion.account_id),
            "cadence": suggestion.cadence.value,
            "amount_cents": str(suggestion.amount_cents),
        },
        "choices": category_choices(db),
    }


def _edit_form(
    db: Session,
    series: RecurringSeries,
    *,
    key: str,
    name: str | None = None,
    cadence: str | None = None,
    amount: str | None = None,
    tolerance: str | None = None,
    category_id: str | None = None,
    error: str = "",
) -> dict[str, Any]:
    current_category = "" if series.category_id is None else str(series.category_id)
    return {
        "series_id": series.id,
        "name": series.name if name is None else name,
        "cadence": series.cadence.value if cadence is None else cadence,
        "amount": plain_dollars(series.amount_cents) if amount is None else amount,
        "tolerance": (
            plain_dollars(series.amount_tolerance_cents) if tolerance is None else tolerance
        ),
        "category_id": current_category if category_id is None else category_id,
        "error": error,
        "key": key,
        "choices": category_choices(db),
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


VERBS = {
    "recurring_confirm": "Confirmed",
    "recurring_dismiss": "Dismissed",
    "recurring_restore": "Restored",
    "recurring_update": "Saved",
    "recurring_delete": "Deleted",
    "recurring_refresh": "Refreshed",
}


def _replay(request: Request, db: Session, idempotency_key: str) -> HTMLResponse | None:
    """A key that already applied answers with its batch. A double click then shows the one change
    instead of a refusal for a suggestion that is no longer there."""
    if not idempotency_key:
        return None
    batch = applied_batch_for_key(db, idempotency_key)
    if batch is None:
        return None
    name = (batch.summary or {}).get("name", "series")
    return _saved(request, batch, f"{VERBS.get(batch.kind, 'Saved')} {name}")


def _saved_refusal(request: Request, message: str, status: int) -> HTMLResponse:
    """A write that did not apply, shown in the toast region."""
    return _block(request, "frag_saved", status=status, message=message, batch_id=None)


def _drawer_saved(
    request: Request,
    db: Session,
    series: RecurringSeries,
    message: str,
    batch: Batch | None,
) -> HTMLResponse:
    """A saved edit: the drawer shows the series as it now is, and the toast carries Undo."""
    db.refresh(series)
    return _block(
        request,
        "frag_drawer_saved",
        trigger={CHANGED: True},
        drawer=build_drawer(db, series, _today(), None),
        message=message,
        batch_id=batch.id if batch is not None else None,
    )


def _refused(exc: Exception) -> int:
    """409 when the data moved on since the page was drawn, 422 for anything the form can fix."""
    return 409 if isinstance(exc, StalePreview) else 422
