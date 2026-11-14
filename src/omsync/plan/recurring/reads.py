"""Amounts and schedule reads of a stored series."""

from datetime import date, timedelta
from typing import Final, Literal

from sqlalchemy.orm import Session

from omsync.plan.models import RecurringSeries
from omsync.plan.recurring.base import MIN_TOLERANCE_CENTS, OccurrenceStatus
from omsync.plan.recurring.calendar import _occurrences
from omsync.plan.recurring.detection import _prices, _read, amount_on, series_transactions


def fixed_tolerance_cents(amount_cents: int) -> int:
    """The automatic tolerance of a fixed amount: the larger of a dollar and 3% of its size, with
    the 3% rounded half up."""
    return max(MIN_TOLERANCE_CENTS, (3 * abs(amount_cents) + 50) // 100)


def observed_tolerance_cents(session: Session, series: RecurringSeries) -> int:
    """How far the series' charges have strayed from the amount in effect on their dates: the
    largest such deviation, and never less than the fixed tolerance. A Varies amount shows this."""
    ledger = _read(session, series)
    deviations = [
        abs(abs(txn.amount_cents) - abs(amount_on(series, txn.posted_on, ledger.prices)))
        for txn in ledger.charges
    ]
    return max([fixed_tolerance_cents(series.amount_cents), *deviations])


def amount_mode(series: RecurringSeries) -> Literal["fixed", "varies"]:
    """Fixed when the stored tolerance is no wider than the automatic one, else varies."""
    if series.amount_tolerance_cents <= fixed_tolerance_cents(series.amount_cents):
        return "fixed"
    return "varies"


def price_history(session: Session, series: RecurringSeries) -> list[tuple[date | None, int]]:
    """The series' amounts, oldest first: its own amount with no date, then each price row by the
    date it took effect."""
    return [
        (None, series.amount_cents),
        *((price.effective_on, price.amount_cents) for price in _prices(session, series.id)),
    ]


def last_seen(session: Session, series: RecurringSeries, *, today: date) -> date | None:
    """The day of the latest matched charge posted on or before `today`, or None when none has."""
    posted = [
        txn.posted_on
        for txn in series_transactions(session, series, since=None)
        if txn.posted_on <= today
    ]
    return max(posted, default=None)


_NEXT_DUE_SEARCH_DAYS: Final = 3 * 366  # long enough to reach the next due date of a yearly series


def next_due(session: Session, series: RecurringSeries, *, today: date) -> date | None:
    """The first due date on or after `today` that is not paid yet, or None when there is none,
    as after an end. Reads only."""
    found = _occurrences(
        session,
        series,
        start=today,
        end=today + timedelta(days=_NEXT_DUE_SEARCH_DAYS),
        today=today,
    )
    return next((item.due_on for item in found if item.status is OccurrenceStatus.upcoming), None)
