"""Recurring writes. Each one is a batch, and a repeated idempotency key returns the first batch."""

import re
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import Change, snapshot
from omsync.ledger.models import Account, Category, Transaction
from omsync.plan.models import (
    Cadence,
    RecurringPaidElsewhere,
    RecurringPrice,
    RecurringRejection,
    RecurringSeries,
    SeriesStatus,
)
from omsync.plan.recurring.base import UNCHANGED, SeriesSuggestion, _Unchanged
from omsync.plan.recurring.calendar import _due_dates
from omsync.plan.recurring.detection import (
    _escape,
    _grid,
    _is_same_series,
    _prices,
    _read,
    _rows_with_pattern,
    _same_bill,
    current_amount,
    payee_key,
)
from omsync.plan.recurring.helpers import _check_date, _check_int, _series_or_raise, _write
from omsync.plan.recurring.reads import fixed_tolerance_cents


def _series_values(
    session: Session, suggestion: SeriesSuggestion, status: SeriesStatus
) -> dict[str, Any]:
    """The row a decision inserts. starts_on is the earliest charge the suggestion was built from,
    not its next due date: the schedule starts where the series was first seen, and a next due date
    is a day still to come."""
    first = session.scalar(
        select(func.min(Transaction.posted_on)).where(Transaction.id.in_(suggestion.occurrences))
    )
    return {
        "name": suggestion.name,
        "payee_pattern": suggestion.payee_pattern,
        "account_id": suggestion.account_id,
        "category_id": suggestion.category_id,
        "cadence": suggestion.cadence,
        "amount_cents": suggestion.amount_cents,
        "amount_tolerance_cents": suggestion.amount_tolerance_cents,
        "starts_on": first,
        "ended_on": None,
        "status": status,
    }


def _decision_changes(
    session: Session, suggestion: SeriesSuggestion, status: SeriesStatus
) -> list[Change]:
    """Insert the decided series. A restored suggestion row for the same pattern is replaced, so no
    stale copy is left behind. Deciding a pattern twice is refused."""
    rows = [
        row
        for row in _rows_with_pattern(session, suggestion.payee_pattern)
        if _is_same_series(row, suggestion, current_amount(session, row))
    ]
    decided = [row for row in rows if row.status is not SeriesStatus.suggested]
    if decided:
        raise ValueError(
            f"{suggestion.name} is already {decided[0].status.value}, so it cannot be decided again"
        )
    superseded = [row for row in rows if row.status is SeriesStatus.suggested]
    changes = [
        Change("recurring_series", str(row.id), snapshot(session, "recurring_series", row.id), None)
        for row in superseded
    ]
    changes.append(
        Change("recurring_series", None, None, _series_values(session, suggestion, status))
    )
    return changes


def confirm(session: Session, suggestion: SeriesSuggestion, *, idempotency_key: str) -> Batch:
    """Save a suggestion as a confirmed series, so it is projected onto the calendar."""
    return _write(
        session,
        "recurring_confirm",
        idempotency_key,
        {"name": suggestion.name, "cadence": suggestion.cadence.value},
        lambda: _decision_changes(session, suggestion, SeriesStatus.confirmed),
    )


def dismiss(session: Session, suggestion: SeriesSuggestion, *, idempotency_key: str) -> Batch:
    """Remember a suggestion as not recurring, so detection never suggests it again. Undo the
    batch, or restore it with restore_dismissed, to bring it back."""
    return _write(
        session,
        "recurring_dismiss",
        idempotency_key,
        {"name": suggestion.name, "cadence": suggestion.cadence.value},
        lambda: _decision_changes(session, suggestion, SeriesStatus.dismissed),
    )


def restore_dismissed(session: Session, series_id: int, *, idempotency_key: str) -> Batch:
    """Delete a dismissed series' row. Removing the dismissal lets detection suggest the pattern
    again. Undo the batch to dismiss it again."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status is not SeriesStatus.dismissed:
            raise ValueError(f"series {series_id} is {row.status.value}, not dismissed")
        before = snapshot(session, "recurring_series", series_id)
        return [Change("recurring_series", str(series_id), before, None)]

    return _write(session, "recurring_restore", idempotency_key, {"series_id": series_id}, build)


def update_series(
    session: Session,
    series_id: int,
    *,
    idempotency_key: str,
    name: str | None = None,
    cadence: Cadence | str | None = None,
    amount_cents: int | None = None,
    amount_tolerance_cents: int | None = None,
    category_id: int | _Unchanged | None = UNCHANGED,
) -> Batch:
    """Edit a series. Fields left at their default keep their value. category_id can be set to
    None, which clears it. amount_cents replaces the amount before any price row."""

    def build() -> list[Change]:
        _series_or_raise(session, series_id)
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = _checked_name(name)
        if cadence is not None:
            changes["cadence"] = Cadence(cadence)
        if amount_cents is not None:
            _check_amount(amount_cents)
            changes["amount_cents"] = amount_cents
        if amount_tolerance_cents is not None:
            _check_tolerance(amount_tolerance_cents)
            changes["amount_tolerance_cents"] = amount_tolerance_cents
        if not isinstance(category_id, _Unchanged):
            _check_category(session, category_id)
            changes["category_id"] = category_id
        before = snapshot(session, "recurring_series", series_id)
        return [Change("recurring_series", str(series_id), before, {**before, **changes})]

    return _write(session, "recurring_update", idempotency_key, {"series_id": series_id}, build)


def edited_amount(series: RecurringSeries, magnitude_cents: int) -> int:
    """The signed amount for an edit that gives a magnitude. A bill stays a bill and a paycheck
    stays income, so editing a series never turns an outflow into an inflow."""
    return magnitude_cents if series.amount_cents > 0 else -magnitude_cents


def delete_series(session: Session, series_id: int, *, idempotency_key: str) -> Batch:
    """Delete a series with its amount history, rejections, and paid-elsewhere marks. The children
    are deleted first in the same batch, so undo restores them. Deleting a dismissed series also
    lets detection suggest it again."""

    def build() -> list[Change]:
        _series_or_raise(session, series_id)
        changes: list[Change] = []
        for model, table in (
            (RecurringPrice, "recurring_price"),
            (RecurringRejection, "recurring_rejection"),
            (RecurringPaidElsewhere, "recurring_paid_elsewhere"),
        ):
            child_ids = session.scalars(
                select(model.id).where(model.series_id == series_id).order_by(model.id)
            ).all()
            for child_id in child_ids:
                before = snapshot(session, table, child_id)
                changes.append(Change(table, str(child_id), before, None))
        before = snapshot(session, "recurring_series", series_id)
        changes.append(Change("recurring_series", str(series_id), before, None))
        return changes

    return _write(
        session,
        "recurring_delete",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def end_series(session: Session, series_id: int, *, ended_on: date, idempotency_key: str) -> Batch:
    """Stop a confirmed series after `ended_on`, the last day it is due. It has no occurrences
    after that day. A matching charge posted later is a question in Needs attention, not a
    revival. Refused unless the series is confirmed."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status is not SeriesStatus.confirmed:
            raise ValueError(f"{row.name} is {row.status.value}, so it cannot be ended")
        _check_date(ended_on, "the end date")
        before = snapshot(session, "recurring_series", series_id)
        after = {**before, "status": SeriesStatus.ended, "ended_on": ended_on}
        return [Change("recurring_series", str(series_id), before, after)]

    return _write(
        session,
        "recurring_end",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id, "ended_on": ended_on},
        build,
    )


def resume_series(session: Session, series_id: int, *, idempotency_key: str) -> Batch:
    """Make an ended series active again. Refused unless the series has ended."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status is not SeriesStatus.ended:
            raise ValueError(f"{row.name} is {row.status.value}, not ended")
        before = snapshot(session, "recurring_series", series_id)
        after = {**before, "status": SeriesStatus.confirmed, "ended_on": None}
        return [Change("recurring_series", str(series_id), before, after)]

    return _write(
        session,
        "recurring_resume",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def accept_new_amount(
    session: Session, series_id: int, transaction_id: int, *, idempotency_key: str
) -> Batch:
    """Take a charge's amount as the series' amount from that charge's posted date. Earlier months
    keep the amount they were due at, and the occurrence the charge paid stays paid.

    Refused when the series is not confirmed, when the charge is not a charge of the series, when
    the user rejected it, or when a price is already set from that date: one date holds one amount.
    """

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status is not SeriesStatus.confirmed:
            raise ValueError(f"{row.name} is {row.status.value}, not confirmed")
        txn = _series_charge_or_raise(session, row, transaction_id)
        if _is_rejected(session, series_id, transaction_id):
            raise ValueError("That charge is marked Not this bill, so it cannot set the amount.")
        if any(price.effective_on == txn.posted_on for price in _prices(session, series_id)):
            raise ValueError(f"A new amount is already set from {txn.posted_on.isoformat()}.")
        values = {
            "series_id": series_id,
            "effective_on": txn.posted_on,
            "amount_cents": txn.amount_cents,
        }
        return [Change("recurring_price", None, None, values)]

    return _write(
        session,
        "recurring_accept_amount",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def reject_charge(
    session: Session, series_id: int, transaction_id: int, *, idempotency_key: str
) -> Batch:
    """Mark a charge as not this bill. It never matches the series again, so the occurrence it
    would have paid is judged without it: missed unless another charge pays it or it is marked paid
    elsewhere. Works on ended series too, for a charge posted after the end."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        _series_charge_or_raise(session, row, transaction_id)
        if _is_rejected(session, series_id, transaction_id):
            raise ValueError("That charge is already marked Not this bill.")
        values = {"series_id": series_id, "transaction_id": transaction_id}
        return [Change("recurring_rejection", None, None, values)]

    return _write(
        session,
        "recurring_reject",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def mark_paid_elsewhere(
    session: Session, series_id: int, due_on: date, *, idempotency_key: str
) -> Batch:
    """Mark one due date of a series paid from an account omsync does not track. It covers that
    due date only. Refused when `due_on` is not a due date of the series' schedule, or is after the
    series ended."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status not in (SeriesStatus.confirmed, SeriesStatus.ended):
            raise ValueError(f"{row.name} is {row.status.value}, so it has no due dates to mark")
        _check_date(due_on, "the due date")
        if row.ended_on is not None and due_on > row.ended_on:
            raise ValueError(f"{row.name} ended on {row.ended_on.isoformat()}")
        ledger = _read(session, row)
        anchor, phase, _ = _grid(row, ledger)
        if not _due_dates(phase, row.cadence, anchor, low=due_on, high=due_on):
            raise ValueError(f"{due_on.isoformat()} is not a due date of {row.name}")
        if due_on in ledger.paid_elsewhere:
            raise ValueError(f"{due_on.isoformat()} is already marked paid elsewhere")
        values = {"series_id": series_id, "due_on": due_on}
        return [Change("recurring_paid_elsewhere", None, None, values)]

    return _write(
        session,
        "recurring_paid_elsewhere",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def add_series(
    session: Session,
    *,
    name: str,
    payee_pattern: str,
    account_id: int | None,
    cadence: Cadence | str,
    amount_cents: int,
    starts_on: date,
    category_id: int | None = None,
    amount_tolerance_cents: int | None = None,
    idempotency_key: str,
) -> Batch:
    """Add a bill the user typed in, confirmed at once. Its tolerance defaults to the fixed one for
    its amount. A pattern that already covers this bill is refused, unless the only such row is an
    undecided suggestion, which the new series replaces."""

    def build() -> list[Change]:
        _checked_name(name)
        _check_payee_pattern(payee_pattern)
        kind = Cadence(cadence)
        _check_amount(amount_cents)
        tolerance = (
            fixed_tolerance_cents(amount_cents)
            if amount_tolerance_cents is None
            else amount_tolerance_cents
        )
        _check_tolerance(tolerance)
        _check_date(starts_on, "the start date")
        if account_id is not None and session.get(Account, account_id) is None:
            raise ValueError(f"there is no account {account_id}")
        _check_category(session, category_id)
        changes = _replaced_suggestions(session, payee_pattern, account_id, kind, amount_cents)
        values = {
            "name": name.strip(),
            "payee_pattern": payee_pattern,
            "account_id": account_id,
            "category_id": category_id,
            "cadence": kind,
            "amount_cents": amount_cents,
            "amount_tolerance_cents": tolerance,
            "starts_on": starts_on,
            "ended_on": None,
            "status": SeriesStatus.confirmed,
        }
        changes.append(Change("recurring_series", None, None, values))
        return changes

    return _write(
        session,
        "recurring_add",
        idempotency_key,
        {"name": name.strip(), "cadence": Cadence(cadence).value},
        build,
    )


def _checked_name(name: str) -> str:
    if not name.strip():
        raise ValueError("Give the bill a name.")
    return name.strip()


def _check_payee_pattern(payee_pattern: str) -> None:
    if not payee_pattern:
        raise ValueError("Enter a payee pattern.")
    try:
        re.compile(payee_pattern)
    except re.error as exc:
        raise ValueError(f"The payee pattern is not valid: {exc}.") from None


def _check_amount(amount_cents: int) -> None:
    _check_int(amount_cents, "a series amount")
    if amount_cents == 0:
        raise ValueError("Enter an amount other than zero.")


def _check_tolerance(tolerance_cents: int) -> None:
    _check_int(tolerance_cents, "a series tolerance")
    if tolerance_cents < 0:
        raise ValueError("The tolerance cannot be negative.")


def _check_category(session: Session, category_id: int | None) -> None:
    if category_id is not None and session.get(Category, category_id) is None:
        raise ValueError(f"there is no category {category_id}")


def _replaced_suggestions(
    session: Session, payee_pattern: str, account_id: int | None, kind: Cadence, amount_cents: int
) -> list[Change]:
    """Deletes of the undecided suggestions the new series replaces. Another series that covers
    the bill refuses it."""
    changes: list[Change] = []
    for row in _rows_with_pattern(session, payee_pattern):
        current = current_amount(session, row)
        if not _same_bill(row, payee_pattern, account_id, kind, amount_cents, current):
            continue
        if row.status is not SeriesStatus.suggested:
            raise ValueError(f"{row.name} already covers this bill, and it is {row.status.value}")
        before = snapshot(session, "recurring_series", row.id)
        changes.append(Change("recurring_series", str(row.id), before, None))
    return changes


def payee_pattern_for(payee_raw: str | None) -> str:
    """The payee pattern detection would give a raw payee, so a bill started from a transaction
    matches the charges a suggestion would. Raises ValueError when the payee leaves no key."""
    key = payee_key(payee_raw)
    if not key:
        raise ValueError("This transaction has no payee to build a pattern from.")
    return _escape(key)


def _series_charge_or_raise(
    session: Session, series: RecurringSeries, transaction_id: int
) -> Transaction:
    """The transaction, when it has the series' payee, sign, and account. Rejection and price are
    the caller's concern."""
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise ValueError(f"there is no transaction {transaction_id}")
    if (txn.amount_cents > 0) != (series.amount_cents > 0):
        raise ValueError(
            f"That charge goes the other way from {series.name}: "
            "one is money in, the other money out."
        )
    if series.account_id is not None and txn.account_id != series.account_id:
        raise ValueError(f"that charge is not in the account of {series.name}")
    if re.compile(series.payee_pattern).fullmatch(payee_key(txn.payee_raw)) is None:
        raise ValueError(f"that charge's payee is not {series.name}'s payee")
    return txn


def _is_rejected(session: Session, series_id: int, transaction_id: int) -> bool:
    return (
        session.scalar(
            select(RecurringRejection.id).where(
                RecurringRejection.series_id == series_id,
                RecurringRejection.transaction_id == transaction_id,
            )
        )
        is not None
    )


def _summary_name(session: Session, series_id: int) -> str:
    """The name a batch summary records. A deleted series keeps its batch, so it may be gone."""
    row = session.get(RecurringSeries, series_id)
    return row.name if row is not None else "series"
