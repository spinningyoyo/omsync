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
from omsync.ledger.counterparties import NothingChanged as CounterpartyUnchanged
from omsync.ledger.counterparties import update_counterparty
from omsync.ledger.links import CounterpartyKind, LinkKind
from omsync.ledger.sources import Account
from omsync.ledger.transactions import Transaction
from omsync.receipts.service import apply_digest, apply_report, registered_vendors, run_vendor
from omsync.reconcile.decisions import (
    MANUAL_KINDS,
    NothingChanged,
    StaleProposal,
    acknowledge,
    acknowledge_counterparty,
    approve,
    manual_link,
    reject,
    unacknowledge,
    unlink,
)
from omsync.reconcile.not_matched import counterparty_choices
from omsync.web.deps import get_db
from omsync.web.reconcile_view import (
    MANUAL_KIND_LABELS,
    Results,
    active_preset,
    default_amount,
    default_range,
    link_candidates,
    open_remaining,
    plain_amount,
    presets,
    results_for,
    side_of,
)
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/reconcile.html"
FRAGMENTS = "reconcile/fragments.html"
STALE = "Results changed since you ran this; run again"
UNDO_CONFLICT = "Can't undo: changed since this was made."
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
        first = date.fromisoformat(start or "")
        last = date.fromisoformat(end or "")
    except ValueError:
        raise RangeError("Enter both dates, such as 2026-07-01.") from None
    if last < first:
        raise RangeError("The end date is before the start date.")
    return first, last


# Pages


@router.get("/reconcile", response_class=HTMLResponse)
def reconcile_page(
    request: Request, db: DbSession, start: str | None = None, end: str | None = None
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
    context.update(
        start=first.isoformat(),
        end=last.isoformat(),
        presets=presets(today),
        active_preset=active_preset(today, first, last),
        results=results,
        notice=notice,
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
) -> HTMLResponse:
    return _decide(request, db, "approve", start, end, signature or [], idempotency_key)


@router.post("/reconcile/reject", response_class=HTMLResponse)
def reject_rows(
    request: Request,
    db: DbSession,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    signature: Annotated[list[str] | None, Form()] = None,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    return _decide(request, db, "reject", start, end, signature or [], idempotency_key)


def _decide(
    request: Request,
    db: Session,
    action: str,
    start_text: str,
    end_text: str,
    signatures: list[str],
    key: str,
) -> HTMLResponse:
    try:
        start, end = parse_range(start_text, end_text, date.today())
    except RangeError as exc:
        return _notice(request, str(exc), 422)
    if not signatures:
        return _region(
            request, db, start, end, notice="Choose at least one proposal first.", status=422
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
        return _region(request, db, start, end, notice=str(exc), status=409)
    except (ValueError, BatchError) as exc:
        return _region(request, db, start, end, notice=str(exc), status=422)
    return _region(request, db, start, end, toast=_toast(batch, batch.summary))


@router.post("/reconcile/receipts/{vendor_key}/apply", response_class=HTMLResponse)
def apply_receipts(
    request: Request,
    db: DbSession,
    vendor_key: str,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    expected: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
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
        return _region(request, db, start_date, end_date, toast=_toast(replay, replay.summary))
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
        return _region(request, db, start_date, end_date, notice="No receipt charge would change.")
    try:
        batch = apply_report(db, report, idempotency_key=key)
    except StalePreview:
        return _stale(request, start_date, end_date)
    except BatchError as exc:
        return _region(request, db, start_date, end_date, notice=str(exc), status=422)
    if batch is None:
        return _region(request, db, start_date, end_date, notice="No receipt charge would change.")
    label = f"Applied {report.to_apply} {vendor_key.title()} receipt charge(s)"
    return _region(request, db, start_date, end_date, toast=_toast(batch, {"label": label}))


@router.post("/reconcile/unlink/{link_id}", response_class=HTMLResponse)
def unlink_link(
    request: Request,
    db: DbSession,
    link_id: int,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _notice(request, str(exc), 422)
    try:
        batch = unlink(db, link_id, idempotency_key=idempotency_key or uuid4().hex)
    except LookupError:
        return _region(
            request, db, start_date, end_date, notice="That link no longer exists.", status=409
        )
    return _region(request, db, start_date, end_date, toast=_toast(batch, batch.summary))


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
            amount=_default_amount_text(remaining, candidates),
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
        amount=_default_amount_text(remaining, candidates),
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
) -> HTMLResponse:
    """Link two transactions by hand. A refusal shows its message in the dialog."""
    source, _ = _transaction_or_404(db, txn)
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _dialog_error(request, str(exc), 422)
    if other is None:
        return _dialog_error(request, "Choose the transaction on the other side.", 422)
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
    return _dialog_done(request, db, start_date, end_date, batch)


# Acknowledgments


@router.get("/reconcile/acknowledge", response_class=HTMLResponse)
def acknowledge_dialog(
    request: Request,
    db: DbSession,
    txn: int,
    scope: str = "transaction",
    start: str = "",
    end: str = "",
) -> HTMLResponse:
    """The dialog body for one transaction: a note for this transaction alone, or the choice of
    counterparty that the standing acknowledgment covers."""
    source, account = _transaction_or_404(db, txn)
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _dialog_error(request, str(exc), 422)
    context: dict[str, Any] = {
        "source": side_of(source, account),
        "txn": txn,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "idempotency_key": uuid4().hex,
    }
    if scope == "counterparty":
        context.update(
            choices=counterparty_choices(db, source),
            kinds=[(kind.value, kind.value.title()) for kind in CounterpartyKind],
        )
        return _fragment(request, "frag_ack_counterparty", **context)
    return _fragment(request, "frag_ack_transaction", **context)


@router.post("/reconcile/acknowledge", response_class=HTMLResponse)
def acknowledge_submit(
    request: Request,
    db: DbSession,
    txn: Annotated[int, Form()],
    note: Annotated[str, Form()] = "",
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Say no counterpart is expected for one transaction. A refusal shows in the dialog."""
    _transaction_or_404(db, txn)
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _dialog_error(request, str(exc), 422)
    try:
        batch = acknowledge(db, txn, note=note, idempotency_key=idempotency_key or uuid4().hex)
    except NothingChanged as exc:
        return _dialog_error(request, str(exc), 409)
    except (ValueError, BatchError) as exc:
        return _dialog_error(request, str(exc), 422)
    return _dialog_done(request, db, start_date, end_date, batch)


@router.post("/reconcile/acknowledge/counterparty", response_class=HTMLResponse)
def acknowledge_counterparty_submit(
    request: Request,
    db: DbSession,
    txn: Annotated[int, Form()],
    counterparty: Annotated[int | None, Form()] = None,
    name: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = CounterpartyKind.business.value,
    pattern: Annotated[str, Form()] = "",
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Say no counterpart is expected from a counterparty: a saved one, or a new one made here."""
    _transaction_or_404(db, txn)
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _dialog_error(request, str(exc), 422)
    try:
        batch = acknowledge_counterparty(
            db,
            txn,
            counterparty_id=counterparty,
            name=name,
            kind=kind,
            pattern=pattern,
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except CounterpartyUnchanged as exc:
        return _dialog_error(request, str(exc), 409)
    except LookupError as exc:
        return _dialog_error(request, str(exc), 422)
    except (ValueError, BatchError) as exc:
        return _dialog_error(request, str(exc), 422)
    return _dialog_done(request, db, start_date, end_date, batch)


@router.post("/reconcile/acknowledgments/{acknowledgment_id}/remove", response_class=HTMLResponse)
def remove_acknowledgment(
    request: Request,
    db: DbSession,
    acknowledgment_id: int,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _notice(request, str(exc), 422)
    try:
        batch = unacknowledge(db, acknowledgment_id, idempotency_key=idempotency_key or uuid4().hex)
    except LookupError:
        return _region(
            request,
            db,
            start_date,
            end_date,
            notice="That acknowledgment no longer exists.",
            status=409,
        )
    return _region(request, db, start_date, end_date, toast=_toast(batch, batch.summary))


@router.post(
    "/reconcile/counterparties/{counterparty_id}/expect-match", response_class=HTMLResponse
)
def expect_match_again(
    request: Request,
    db: DbSession,
    counterparty_id: int,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Remove a counterparty's standing acknowledgment. The counterparty itself stays."""
    try:
        start_date, end_date = parse_range(start, end, date.today())
    except RangeError as exc:
        return _notice(request, str(exc), 422)
    try:
        batch = update_counterparty(
            db,
            counterparty_id,
            no_match_expected=False,
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except (LookupError, CounterpartyUnchanged):
        return _region(
            request,
            db,
            start_date,
            end_date,
            notice="That counterparty is no longer acknowledged.",
            status=409,
        )
    return _region(request, db, start_date, end_date, toast=_toast(batch, batch.summary))


# Undo


def undo_reply(request: Request, db: Session, start: str | None, end: str | None) -> HTMLResponse:
    """After an undo of a reconcile or receipts batch: the toast, and the results re-run.

    Called from the shared undo route. The range comes from the undo URL, and the default range
    applies when it names none.
    """
    today = date.today()
    try:
        start_date, end_date = parse_range(start, end, today)
    except RangeError:
        start_date, end_date = default_range(today)
    return _fragment(
        request,
        "frag_undo_reply",
        message="Undone",
        results=results_for(
            db, request.app.state.settings, start=start_date, end=end_date, today=today
        ),
        start=start_date.isoformat(),
        end=end_date.isoformat(),
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


def _default_amount_text(remaining: int, candidates: list[Any]) -> str:
    if not candidates:
        return plain_amount(remaining)
    return plain_amount(default_amount(remaining, candidates[0].remaining))


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
    )


def _dialog_done(
    request: Request, db: Session, start: date, end: date, batch: Batch
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
