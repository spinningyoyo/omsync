"""Calendar occurrences, month totals, and what needs attention."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.months import month_end
from omsync.ledger.models import Transaction
from omsync.plan.models import Cadence, RecurringSeries, SeriesStatus
from omsync.plan.recurring.base import _SCHEDULES, Occurrence, OccurrenceFlag, OccurrenceStatus
from omsync.plan.recurring.detection import (
    _grid,
    _prices,
    _read,
    _step,
    amount_on,
    is_transfer,
    series_transactions,
)


@dataclass(frozen=True)
class MonthTotals:
    """The headline totals of one month, all magnitudes. Left to pay is the outflows still
    upcoming, with how many there are. Paid is what the paid outflows actually posted. Income
    expected is every inflow due this month, paid or not."""

    left_to_pay_cents: int
    left_to_pay_count: int
    paid_cents: int
    income_expected_cents: int


def month_totals(occurrences: Iterable[Occurrence]) -> MonthTotals:
    """Totals for the occurrences of one month. A missed outflow counts toward neither left to
    pay nor paid: it is listed as needing attention instead. A transfer counts toward nothing,
    since its money is already counted in the ledger."""
    left = left_count = paid = income = 0
    for occurrence in occurrences:
        if occurrence.is_transfer:
            continue
        if occurrence.expected_cents > 0:
            income += occurrence.expected_cents
        elif occurrence.status is OccurrenceStatus.paid:
            paid += abs(
                occurrence.expected_cents
                if occurrence.actual_cents is None
                else occurrence.actual_cents
            )
        elif occurrence.status is OccurrenceStatus.upcoming:
            left += -occurrence.expected_cents
            left_count += 1
    return MonthTotals(left, left_count, paid, income)


def upcoming(session: Session, *, start: date, end: date, today: date) -> list[Occurrence]:
    """The due dates of confirmed and ended series within [start, end], oldest first.

    Each is paid when a charge within the series' timing window of the due date pays it, or when
    it is marked paid elsewhere, and missed when its window closed with neither. Otherwise it is
    upcoming. An ended series is due only up to its ended_on. Reads only.
    """
    rows = session.scalars(
        select(RecurringSeries)
        .where(RecurringSeries.status.in_([SeriesStatus.confirmed, SeriesStatus.ended]))
        .order_by(RecurringSeries.id)
    ).all()
    found: list[Occurrence] = []
    for row in rows:
        found.extend(_occurrences(session, row, start=start, end=end, today=today))
    return sorted(found, key=lambda o: (o.due_on, o.series_name, o.series_id))


class AttentionKind(StrEnum):
    missed = "missed"  # the window closed with no charge and no paid-elsewhere mark
    new_amount = "new_amount"  # a charge outside the band paid the due date
    ambiguous = "ambiguous"  # more than one charge could have paid the due date
    charged_after_end = "charged_after_end"  # a matching charge posted after the series ended


@dataclass(frozen=True)
class AttentionItem:
    """One item for Needs attention. `day` is the due date, or the posted date for a charge after an
    end. expected_cents is the amount in effect that day. actual_cents and transaction_id name the
    charge the question is about, if there is one, and other_transaction_ids the rest of an
    ambiguous pair."""

    kind: AttentionKind
    series_id: int
    series_name: str
    day: date
    expected_cents: int
    actual_cents: int | None
    transaction_id: int | None
    other_transaction_ids: tuple[int, ...]


def needs_attention(session: Session, *, today: date) -> list[AttentionItem]:
    """What needs the person's attention this calendar month, by day, then name. That is the missed
    occurrences whose window closed, the open questions about this month's due dates, and the
    matching charges posted after a series ended. Reads only."""
    month = today.replace(day=1)
    last = month_end(month)
    items: list[AttentionItem] = []
    for occurrence in upcoming(session, start=month, end=last, today=today):
        if occurrence.status is OccurrenceStatus.missed:
            items.append(
                AttentionItem(
                    AttentionKind.missed,
                    occurrence.series_id,
                    occurrence.series_name,
                    occurrence.due_on,
                    occurrence.expected_cents,
                    None,
                    None,
                    (),
                )
            )
        elif occurrence.flag is not None:
            kind = (
                AttentionKind.new_amount
                if occurrence.flag is OccurrenceFlag.new_amount
                else AttentionKind.ambiguous
            )
            items.append(
                AttentionItem(
                    kind,
                    occurrence.series_id,
                    occurrence.series_name,
                    occurrence.due_on,
                    occurrence.expected_cents,
                    occurrence.actual_cents,
                    occurrence.transaction_id,
                    occurrence.other_transaction_ids,
                )
            )
    items.extend(_charged_after_end(session, month=month, last=last))
    return sorted(items, key=lambda item: (item.day, item.series_name, item.series_id, item.kind))


def _charged_after_end(session: Session, *, month: date, last: date) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    ended = session.scalars(
        select(RecurringSeries).where(RecurringSeries.status == SeriesStatus.ended)
    ).all()
    for series in ended:
        if series.ended_on is None:
            continue
        prices = _prices(session, series.id)
        for txn in series_transactions(session, series, since=month):
            if series.ended_on < txn.posted_on <= last:
                items.append(  # noqa: PERF401 - one item built over several lines
                    AttentionItem(
                        AttentionKind.charged_after_end,
                        series.id,
                        series.name,
                        txn.posted_on,
                        amount_on(series, txn.posted_on, prices),
                        txn.amount_cents,
                        txn.id,
                        (),
                    )
                )
    return items


def first_per_series(occurrences: Iterable[Occurrence]) -> list[Occurrence]:
    """The earliest occurrence of each series, in the order given."""
    seen: set[int] = set()
    firsts: list[Occurrence] = []
    for item in occurrences:
        if item.series_id not in seen:
            seen.add(item.series_id)
            firsts.append(item)
    return firsts


def _occurrences(
    session: Session, series: RecurringSeries, *, start: date, end: date, today: date
) -> list[Occurrence]:
    """The occurrences of one series due in [start, end], oldest first.

    A due date is paid by its paid-elsewhere mark, or else by the nearest unused in-band charge in
    its window. Failing both, a lone charge from the same payee outside the band pays it, as a new
    amount for the user to decide (see _price_change). Two or more in-band charges in the window
    make it ambiguous. Otherwise
    it is missed once its window closes, and upcoming until then.
    """
    tolerance = _SCHEDULES[series.cadence].tolerance_days
    window = timedelta(days=tolerance)
    if series.ended_on is not None:
        end = min(end, series.ended_on)
    ledger = _read(session, series)
    anchor, phase, floor = _grid(series, ledger)
    transfer = is_transfer(session, series)
    used: set[int] = set()
    found: list[Occurrence] = []
    for due in _due_dates(phase, series.cadence, anchor, low=max(start, floor), high=end):
        expected = amount_on(series, due, ledger.prices)
        if due in ledger.paid_elsewhere:
            found.append(
                Occurrence(
                    series.id,
                    series.name,
                    due,
                    expected,
                    OccurrenceStatus.paid,
                    paid_elsewhere=True,
                    is_transfer=transfer,
                )
            )
            continue
        matched = _window_candidates(due, ledger.matched, used, tolerance)
        candidates = matched or _price_change(due, expected, ledger.price_changes, used, tolerance)
        if candidates:
            txn = candidates[0]
            used.add(txn.id)
            others = tuple(sorted(item.id for item in candidates[1:]))
            flag: OccurrenceFlag | None = None
            if others:
                flag = OccurrenceFlag.ambiguous
            elif not matched:
                flag = OccurrenceFlag.new_amount
            found.append(
                Occurrence(
                    series.id,
                    series.name,
                    due,
                    expected,
                    OccurrenceStatus.paid,
                    transaction_id=txn.id,
                    posted_on=txn.posted_on,
                    actual_cents=txn.amount_cents,
                    flag=flag,
                    other_transaction_ids=others,
                    is_transfer=transfer,
                )
            )
            continue
        status = OccurrenceStatus.missed if today > due + window else OccurrenceStatus.upcoming
        found.append(
            Occurrence(series.id, series.name, due, expected, status, is_transfer=transfer)
        )
    return found


def _window_candidates(
    due: date, candidates: list[Transaction], used: set[int], tolerance: int
) -> list[Transaction]:
    """The unused candidates posted within the window of `due`, nearest first."""
    return sorted(
        (
            txn
            for txn in candidates
            if txn.id not in used and abs((txn.posted_on - due).days) <= tolerance
        ),
        key=lambda txn: (abs((txn.posted_on - due).days), txn.posted_on, txn.id),
    )


def _price_change(
    due: date, expected: int, candidates: list[Transaction], used: set[int], tolerance: int
) -> list[Transaction]:
    """The one charge outside the band that can stand for a new amount, or none. The payee may
    also sell unrelated things, so a price change needs a single out-of-band charge in the window,
    within half to double the amount in effect. Anything else leaves the due date unpaid."""
    found = _window_candidates(due, candidates, used, tolerance)
    if len(found) != 1:
        return []
    size, base = abs(found[0].amount_cents), abs(expected)
    return found if base <= 2 * size and size <= 2 * base else []


def _due_dates(
    first: date, cadence: Cadence, anchor_day: int, *, low: date, high: date
) -> list[date]:
    """Every due date in [low, high] on the schedule that `first` is on, oldest first."""
    if low > high:
        return []
    day = first
    while day > high:
        day = _step(day, cadence, anchor_day, -1)
    while day < low:
        day = _step(day, cadence, anchor_day, 1)
    if day > high:
        return []
    dues = [day]
    earlier = _step(day, cadence, anchor_day, -1)
    while earlier >= low:
        dues.append(earlier)
        earlier = _step(earlier, cadence, anchor_day, -1)
    later = _step(day, cadence, anchor_day, 1)
    while later <= high:
        dues.append(later)
        later = _step(later, cadence, anchor_day, 1)
    return sorted(dues)
