"""Reconciliation pages and decision responses."""

import json
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError, StalePreview, applied_batch_for_key
from omsync.core.money import to_cents
from omsync.core.months import parse_day
from omsync.ledger.contacts import ContactKind, create_contact, starting_names
from omsync.ledger.counterparty import set_counterparty
from omsync.ledger.links import LinkKind
from omsync.ledger.register import keep_category
from omsync.ledger.sources import Account
from omsync.ledger.transactions import Transaction
from omsync.receipts.service import apply_digest, apply_report, registered_vendors, run_vendor
from omsync.reconcile.decisions import (
    MANUAL_KINDS,
    NothingChanged,
    StaleProposal,
    approve,
    manual_link,
    reject,
    unlink,
)
from omsync.web.automatic import run_automatic, with_automatic
from omsync.web.contacts_view import NEW_CONTACT, parse_choice
from omsync.web.deps import get_db
from omsync.web.reconcile_view import (
    CUSTOM,
    MANUAL_KIND_LABELS,
    Results,
    active_preset,
    default_range,
    link_candidates,
    open_remaining,
    period_label,
    period_options,
    plain_amount,
    results_for,
    side_of,
)
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/reconcile.html"
FRAGMENTS = "reconcile/fragments.html"
STALE = "Results changed since you ran this; run again"
LINK_KINDS = tuple(sorted(kind.value for kind in MANUAL_KINDS))  # the order the dialog lists


class RangeError(ValueError):
    """The dates in the URL or a form do not make a range."""


@dataclass(frozen=True)
class Toast:
    message: str
    batch_id: int


def parse_range(start: str | None, end: str | None, today: date) -> tuple[date, date]:
    """The range a URL names. With neither date, it is the last 90 days."""
    if not start and not end:
        return default_range(today)
    try:
        first = parse_day(start or "")
        last = parse_day(end or "")
    except ValueError:
        raise RangeError("Enter both dates between 1900 and 2199, such as 2026-07-01.") from None
    if last < first:
        raise RangeError("The end date is before the start date.")
    return first, last


# Pages


@router.get("/reconcile", response_class=HTMLResponse)
def reconcile_page(
    request: Request,
    db: DbSession,
    start: str | None = None,
    end: str | None = None,
    period: str | None = None,
    tab: str = "",
) -> HTMLResponse:
    today = date.today()
    notice = None
    status = 200
    try:
        first, last = parse_range(start, end, today)
    except RangeError as exc:
        first, last = default_range(today)
        notice, status = str(exc), 422
    results = results_for(db, request.app.state.settings, start=first, end=last, today=today)
    context = shell_context(request, db, active="reconcile", title="Reconcile")
    custom = period == CUSTOM
    active = results.active_tab(tab)
    context.update(
        start=first.isoformat(),
        end=last.isoformat(),
        custom=custom or active_preset(today, first, last) is None,
        period_options=period_options(today, first, last, custom=custom, tab=active),
        period_label=period_label(today, first, last, custom=custom),
        results=results,
        notice=notice,
        tab=active,
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context, status_code=status)


# Decisions


@router.post("/reconcile/approve", response_class=HTMLResponse)
def approve_rows(
    request: Request,
    db: DbSession,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    signature: Annotated[list[str] | None, Form()] = None,
    idempotency_key: Annotated[str, Form()] = "",
    tab: Annotated[str, Form()] = "",
) -> HTMLResponse:
    return _decide(request, db, "approve", start, end, signature or [], idempotency_key, tab)


@router.post("/reconcile/reject", response_class=HTMLResponse)
def reject_rows(
    request: Request,
    db: DbSession,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    signature: Annotated[list[str] | None, Form()] = None,
    idempotency_key: Annotated[str, Form()] = "",
    tab: Annotated[str, Form()] = "",
) -> HTMLResponse:
    return _decide(request, db, "reject", start, end, signature or [], idempotency_key, tab)


def _decide(
    request: Request,
    db: Session,
    action: str,
    start_text: str,
    end_text: str,
    signatures: list[str],
    key: str,
    tab: str,
) -> HTMLResponse:
    try:
        start, end = parse_range(start_text, end_text, date.today())
    except RangeError as exc:
        return _notice(request, str(exc), 422)
    if not signatures:
        return _region(
            request,
            db,
            start,
            end,
            tab=tab,
            notice="Choose at least one proposal first.",
            status=422,
        )
    key = key or uuid4().hex
    try:
        if action == "approve":
            batch = approve(db, start=start, end=end, signatures=signatures, idempotency_key=key)
        else:
            batch = reject(db, signatures=signatures, idempotency_key=key, start=start, end=end)
    except StaleProposal:
        return _stale(request, start, end)
    except StalePreview:
        return _stale(request, start, end)
    except NothingChanged as exc:
        return _region(request, db, start, end, tab=tab, notice=str(exc), status=409)
    except (ValueError, BatchError) as exc:
        return _region(request, db, start, end, tab=tab, notice=str(exc), status=422)
    return _region(request, db, start, end, tab=tab, toast=_toast(batch, batch.summary))


@router.post("/reconcile/receipts/{vendor_key}/apply", response_class=HTMLResponse)
def apply_receipts(
    request: Request,
    db: DbSession,
    vendor_key: str,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    expected: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
    tab: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Apply a vendor's matched receipts, if the operation still matches the one you reviewed.

    `expected` is the digest the page showed, of the preview and the period it was made for. A
    matching count is not enough: the same number of charges can point at other transactions or
    other receipt revisions, and a preview for one period never applies in another."""
    if vendor_key not in {vendor.key for vendor in registered_vendors()}:
        raise HTTPException(status_code=404, detail="No such receipt vendor")
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _notice(request, str(exc), 422)
    key = idempotency_key or uuid4().hex
    replay = applied_batch_for_key(db, key)
    if replay is not None:
        return _region(
            request, db, start_date, end_date, tab=tab, toast=_toast(replay, replay.summary)
        )
    report = run_vendor(
        db,
        request.app.state.settings,
        vendor_key,
        today=date.today(),
        start=start_date,
        end=end_date,
    )
    if apply_digest(report) != expected:
        return _stale(request, start_date, end_date)
    if report.to_apply == 0:
        return _region(
            request, db, start_date, end_date, tab=tab, notice="No receipt charge would change."
        )
    try:
        batch = apply_report(db, report, idempotency_key=key)
    except StalePreview:
        return _stale(request, start_date, end_date)
    except BatchError as exc:
        return _region(request, db, start_date, end_date, tab=tab, notice=str(exc), status=422)
    if batch is None:
        return _region(
            request, db, start_date, end_date, tab=tab, notice="No receipt charge would change."
        )
    label = f"Applied {report.to_apply} {vendor_key.title()} receipt charge(s)"
    return _region(
        request, db, start_date, end_date, tab=tab, toast=_toast(batch, {"label": label})
    )


@router.post("/reconcile/unlink/{link_id}", response_class=HTMLResponse)
def unlink_link(
    request: Request,
    db: DbSession,
    link_id: int,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
    tab: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _notice(request, str(exc), 422)
    try:
        batch = unlink(db, link_id, idempotency_key=idempotency_key or uuid4().hex)
    except LookupError:
        return _region(
            request,
            db,
            start_date,
            end_date,
            tab=tab,
            notice="That link no longer exists.",
            status=409,
        )
    return _region(request, db, start_date, end_date, tab=tab, toast=_toast(batch, batch.summary))


@router.post("/reconcile/keep", response_class=HTMLResponse)
def keep_receipt_category(
    request: Request,
    db: DbSession,
    transaction_id: Annotated[int, Form()],
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
    tab: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Keep the category a receipt's one possible transaction has, so the receipt stops waiting."""
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _notice(request, str(exc), 422)
    try:
        batch = keep_category(db, transaction_id, idempotency_key=idempotency_key or uuid4().hex)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such transaction") from None
    except (ValueError, BatchError) as exc:
        return _region(request, db, start_date, end_date, tab=tab, notice=str(exc), status=422)
    return _region(request, db, start_date, end_date, tab=tab, toast=_toast(batch, batch.summary))


# The counterparty


@router.post("/reconcile/counterparty", response_class=HTMLResponse)
def set_counterparty_rows(
    request: Request,
    db: DbSession,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    txn: Annotated[list[int] | None, Form()] = None,
    target: Annotated[str, Form()] = "",
    new_name: Annotated[str, Form()] = "",
    new_kind: Annotated[str, Form()] = ContactKind.person.value,
    idempotency_key: Annotated[str, Form()] = "",
    tab: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Set the counterparty of the chosen Not matched rows, as one batch, then run the
    automatic step. target is a contact, a contact's account, or NEW_CONTACT with new_name."""
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _notice(request, str(exc), 422)
    if not txn:
        return _form_error(request, "Tick at least one row in the table below first.")
    if not target:
        return _form_error(request, "Choose the counterparty of the ticked rows.")
    key = idempotency_key or uuid4().hex
    try:
        if target == NEW_CONTACT:
            contact_id = _new_contact(db, new_name, new_kind, key)
            account_id = None
        else:
            contact_id, account_id = parse_choice(target)
        batch = set_counterparty(
            db,
            transaction_ids=txn,
            contact_id=contact_id,
            contact_account_id=account_id,
            idempotency_key=key,
        )
    except LookupError as exc:
        return _region(request, db, start_date, end_date, tab=tab, notice=str(exc), status=422)
    except (ValueError, BatchError) as exc:
        return _region(request, db, start_date, end_date, tab=tab, notice=str(exc), status=422)
    label = str((batch.summary or {}).get("label", "Change saved"))
    message = with_automatic(label, run_automatic(db, batch.id))
    return _region(request, db, start_date, end_date, tab=tab, toast=Toast(message, batch.id))


def _new_contact(db: Session, name: str, kind: str, key: str) -> int:
    """Create the contact the rows are being set to, and return its ID. The key derives from the
    request's, so a repeated submit finds the contact it made instead of making another."""
    batch = create_contact(
        db,
        name=name,
        kind=kind,
        names=starting_names(name, kind),
        idempotency_key=f"{key}:new-contact",
    )
    created = (batch.summary or {}).get("contact_id")
    if not isinstance(created, int):
        raise ValueError("The contact could not be created.")
    return created


# Manual links


@router.get("/reconcile/link", response_class=HTMLResponse)
def link_dialog(
    request: Request, db: DbSession, txn: int, start: str = "", end: str = ""
) -> HTMLResponse:
    """The dialog body for one transaction: its open amount and the first candidates."""
    source, account = _transaction_or_404(db, txn)
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _dialog_error(request, str(exc), 422)
    remaining = open_remaining(db, txn)
    candidates = link_candidates(
        db,
        source_id=txn,
        source_amount=source.amount_cents,
        query="",
        start=start_date,
        end=end_date,
    )
    return _fragment(
        request,
        "frag_link_dialog",
        **_link_context(
            source,
            account,
            start_date,
            end_date,
            remaining,
            candidates,
            amount=_default_amount_text(remaining),
        ),
    )


@router.get("/reconcile/link/candidates", response_class=HTMLResponse)
def link_search(
    request: Request, db: DbSession, txn: int, q: str = "", start: str = "", end: str = ""
) -> HTMLResponse:
    """The candidate list for a search, and the amount it defaults to."""
    source, _ = _transaction_or_404(db, txn)
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _dialog_error(request, str(exc), 422)
    remaining = open_remaining(db, txn)
    candidates = link_candidates(
        db,
        source_id=txn,
        source_amount=source.amount_cents,
        query=q,
        start=start_date,
        end=end_date,
    )
    return _fragment(
        request,
        "frag_link_candidates",
        candidates=candidates,
        amount=_default_amount_text(remaining),
        remaining=remaining,
    )


@router.post("/reconcile/link", response_class=HTMLResponse)
def link_submit(
    request: Request,
    db: DbSession,
    txn: Annotated[int, Form()],
    other: Annotated[int | None, Form()] = None,
    kind: Annotated[str, Form()] = LinkKind.transfer.value,
    amount: Annotated[str, Form()] = "",
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
    tab: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Link two transactions by hand. A refusal shows its message in the dialog."""
    source, _ = _transaction_or_404(db, txn)
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _dialog_error(request, str(exc), 422)
    if other is None:
        return _dialog_error(request, "Choose the counterpart.", 422)
    try:
        cents = to_cents(amount.replace("$", "").strip() or "0")
    except (ValueError, TypeError):
        return _dialog_error(request, "Enter the amount in dollars, such as 25.00.", 422)
    money_out, money_in = (txn, other) if source.amount_cents < 0 else (other, txn)
    try:
        batch = manual_link(
            db,
            out_txn_id=money_out,
            in_txn_id=money_in,
            kind=kind,
            amount_cents=cents,
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except LookupError as exc:
        return _dialog_error(request, str(exc), 422)
    except ValueError as exc:
        return _dialog_error(request, str(exc), 422)
    except BatchError as exc:
        return _dialog_error(request, str(exc), 409)
    return _dialog_done(request, db, start_date, end_date, batch, tab)


# Undo


def undo_reply(
    request: Request,
    db: Session,
    start: str | None,
    end: str | None,
    *,
    automatic: bool = False,
    message: str = "Undone",
) -> HTMLResponse:
    """After an undo of a reconcile or receipts batch: the toast, and the results re-run.

    Called from the shared undo route. The range comes from the undo URL, and the default range
    applies when it names none. After an undo of the counterparty, `automatic` runs the automatic
    step again, because what it identified rested on what was undone.
    """
    message = with_automatic(message, run_automatic(db)) if automatic else message
    tab = request.query_params.get("tab", "")  # the toast's Undo names the tab that was open
    today = date.today()
    try:
        start_date, end_date = parse_range(start, end, today)
    except RangeError:
        start_date, end_date = default_range(today)
    return _fragment(
        request,
        "frag_undo_reply",
        message=message,
        results=results_for(
            db, request.app.state.settings, start=start_date, end=end_date, today=today
        ),
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        tab=tab,
    )


# Helpers


def _toast(batch: Batch, summary: dict[str, Any] | None) -> Toast:
    label = (summary or {}).get("label", "Change saved")
    return Toast(message=str(label), batch_id=batch.id)


def _transaction_or_404(db: Session, txn_id: int) -> tuple[Transaction, Account]:
    row = db.execute(
        select(Transaction, Account)
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.id == txn_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No such transaction")
    return row[0], row[1]


def _default_amount_text(remaining: int) -> str:
    """The amount a new link starts at: all of the source's open part. Choosing a candidate lowers
    it to that candidate's open part when it is smaller (reconcile_controller.js)."""
    return plain_amount(remaining)


def _link_context(
    source: Transaction,
    account: Account,
    start: date,
    end: date,
    remaining: int,
    candidates: list[Any],
    *,
    amount: str,
) -> dict[str, Any]:
    return {
        "source": side_of(source, account),
        "source_remaining": remaining,
        "source_remaining_text": plain_amount(remaining),
        "candidates": candidates,
        "amount": amount,
        "kinds": [(value, MANUAL_KIND_LABELS[value]) for value in LINK_KINDS],
        "default_kind": LinkKind.transfer.value,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "idempotency_key": uuid4().hex,
        "txn": source.id,
    }


def _region(
    request: Request,
    db: Session,
    start: date,
    end: date,
    *,
    tab: str = "",
    notice: str | None = None,
    toast: Toast | None = None,
    status: int = 200,
) -> HTMLResponse:
    results: Results = results_for(
        db, request.app.state.settings, start=start, end=end, today=date.today()
    )
    return _fragment(
        request,
        "frag_results",
        status=status,
        results=results,
        notice=notice,
        toast=toast,
        start=start.isoformat(),
        end=end.isoformat(),
        tab=tab,
    )


def _dialog_done(
    request: Request, db: Session, start: date, end: date, batch: Batch, tab: str
) -> HTMLResponse:
    """A saved decision made in a dialog: the dialog closes and the results re-run."""
    return _fragment(
        request,
        "frag_dialog_done",
        trigger={"close-dialogs": True},
        results=results_for(
            db, request.app.state.settings, start=start, end=end, today=date.today()
        ),
        toast=_toast(batch, batch.summary),
        start=start.isoformat(),
        end=end.isoformat(),
        tab=tab,
    )


def _stale(request: Request, start: date, end: date) -> HTMLResponse:
    return _fragment(
        request,
        "frag_stale",
        status=409,
        message=STALE,
        start=start.isoformat(),
        end=end.isoformat(),
    )


def _notice(request: Request, message: str, status: int) -> HTMLResponse:
    return _fragment(request, "frag_notice", status=status, message=message)


def _dialog_error(request: Request, message: str, status: int) -> HTMLResponse:
    return _fragment(request, "frag_dialog_error", status=status, message=message)


def _form_error(request: Request, message: str) -> HTMLResponse:
    """A refusal of the Set counterparty form, shown beside its button. The form otherwise swaps the
    whole results region, so the reply retargets itself and the page does not move."""
    response = _fragment(request, "frag_dialog_error", status=422, message=message)
    response.headers["HX-Retarget"] = "#counterparty-error"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


def _fragment(
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
