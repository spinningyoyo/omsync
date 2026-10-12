"""View models for the Recurring page: the month summary, the upcoming weeks, the month grid, the
series tables, the suggestions, and the series drawer. Reads only. Writes are in
routes/recurring.py.

A bill is negative, as in the ledger. The summary totals are magnitudes, and each one says whether
it counts paid, missed, or still due occurrences.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.core.months import month_end, shift_month_start
from omsync.ledger.filters import Condition, FilterSpec
from omsync.ledger.models import (
    Account,
    Cadence,
    Category,
    RecurringSeries,
    SeriesStatus,
    Transaction,
)
from omsync.plan.recurring import (
    MonthSummary,
    Occurrence,
    OccurrenceStatus,
    SeriesSuggestion,
    detect,
    series_transactions,
    summarize,
    upcoming,
)
from omsync.web.formats import fmt_date, month_key, month_title, parse_month
from omsync.web.register_state import encode_filter

UPCOMING_DAYS = 45
DRAWER_TRANSACTIONS = 6
CADENCE_LABELS: dict[str, str] = {
    Cadence.weekly.value: "Weekly",
    Cadence.biweekly.value: "Biweekly",
    Cadence.monthly.value: "Monthly",
    Cadence.quarterly.value: "Quarterly",
    Cadence.yearly.value: "Yearly",
}
STATE_LABELS: dict[str, str] = {
    OccurrenceStatus.paid.value: "Paid",
    OccurrenceStatus.upcoming.value: "Due",
    OccurrenceStatus.missed.value: "Missed",
}


# Dates and months


def short_day(day: date) -> str:
    return f"{day:%a} {day:%b} {day.day}"


def first_of_month(day: date) -> date:
    return day.replace(day=1)


def viewed_month(text: str | None, today: date) -> date:
    """The first day of the month in a YYYY-MM value. A missing or unreadable value means this
    month."""
    return parse_month(text or "") or first_of_month(today)


def plain_dollars(cents: int) -> str:
    """Cents as a dollars string for a form, such as 4599 as 45.99, with no sign and no float."""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{dollars}.{remainder:02d}"


def transactions_href(payee_pattern: str, account_id: int | None) -> str:
    """A link to the Transactions register for a series: payee contains the pattern's first word,
    and the account is the series' account. The pattern keeps no reference numbers, so the first
    word is the part of the payee the raw text still has. The filter is in the URL as base64url."""
    pattern = re.sub(r"\\(.)", r"\1", payee_pattern).strip()
    words = pattern.split()
    conditions: list[Condition] = [Condition("payee", "contains", words[0] if words else pattern)]
    if account_id is not None:
        conditions.append(Condition("account", "is", [account_id]))
    return "/transactions?f=" + encode_filter(FilterSpec(conditions=tuple(conditions)))


# Occurrences


@dataclass(frozen=True)
class OccurrenceView:
    series_id: int
    name: str
    due_on: date
    expected_cents: int
    state: str  # the OccurrenceStatus value: paid, upcoming, or missed
    actual_cents: int | None
    posted_on: date | None

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def shown_cents(self) -> int:
        return self.actual_cents if self.actual_cents is not None else self.expected_cents

    @property
    def shown_text(self) -> str:
        return format_cents(self.shown_cents)

    @property
    def due_text(self) -> str:
        return short_day(self.due_on)


def occurrence_view(occurrence: Occurrence) -> OccurrenceView:
    return OccurrenceView(
        series_id=occurrence.series_id,
        name=occurrence.series_name,
        due_on=occurrence.due_on,
        expected_cents=occurrence.expected_cents,
        state=occurrence.status.value,
        actual_cents=occurrence.actual_cents,
        posted_on=occurrence.posted_on,
    )


@dataclass(frozen=True)
class WeekGroup:
    label: str
    occurrences: tuple[OccurrenceView, ...]


def upcoming_weeks(occurrences: list[OccurrenceView]) -> list[WeekGroup]:
    """Occurrences grouped by the Monday that starts their week, oldest week first."""
    by_monday: dict[date, list[OccurrenceView]] = {}
    for occurrence in occurrences:
        monday = occurrence.due_on - timedelta(days=occurrence.due_on.weekday())
        by_monday.setdefault(monday, []).append(occurrence)
    return [
        WeekGroup(f"Week of {fmt_date(monday)}", tuple(items))
        for monday, items in sorted(by_monday.items())
    ]


@dataclass(frozen=True)
class CalendarDay:
    day: date
    in_month: bool
    is_today: bool
    occurrences: tuple[OccurrenceView, ...]


def calendar_range(month_start: date) -> tuple[date, date]:
    """The first and last day of the grid: whole Monday-to-Sunday weeks around the month."""
    last = month_end(month_start)
    return (
        month_start - timedelta(days=month_start.weekday()),
        last + timedelta(days=6 - last.weekday()),
    )


def calendar_weeks(
    month_start: date, occurrences: list[OccurrenceView], today: date
) -> list[list[CalendarDay]]:
    start, end = calendar_range(month_start)
    by_day: dict[date, list[OccurrenceView]] = {}
    for occurrence in occurrences:
        by_day.setdefault(occurrence.due_on, []).append(occurrence)
    days = [
        CalendarDay(
            day=start + timedelta(days=offset),
            in_month=(start + timedelta(days=offset)).month == month_start.month,
            is_today=start + timedelta(days=offset) == today,
            occurrences=tuple(by_day.get(start + timedelta(days=offset), ())),
        )
        for offset in range((end - start).days + 1)
    ]
    return [days[index : index + 7] for index in range(0, len(days), 7)]


# Series


@dataclass(frozen=True)
class SeriesRow:
    id: int
    name: str
    cadence: str
    amount_text: str
    amount_cents: int
    tolerance_cents: int
    next_due_text: str
    last_seen_text: str
    category: str
    account: str
    link: str
    key: str = field(default_factory=lambda: uuid4().hex)  # for restore, made per render


def _series_row(series: RecurringSeries, category: str | None, account: str | None) -> SeriesRow:
    tolerance = (
        f" ± {format_cents(series.amount_tolerance_cents)}" if series.amount_tolerance_cents else ""
    )
    return SeriesRow(
        id=series.id,
        name=series.name,
        cadence=CADENCE_LABELS[series.cadence.value],
        amount_text=f"{format_cents(series.amount_cents)}{tolerance}",
        amount_cents=series.amount_cents,
        tolerance_cents=series.amount_tolerance_cents,
        next_due_text=fmt_date(series.next_due) if series.next_due else "Not set",
        last_seen_text=fmt_date(series.last_seen) if series.last_seen else "Never",
        category=category or "Uncategorized",
        account=account or "Any account",
        link=transactions_href(series.payee_pattern, series.account_id),
    )


def series_rows(db: Session, status: SeriesStatus) -> list[SeriesRow]:
    rows = db.execute(
        select(RecurringSeries, Category.name, Account.name)
        .outerjoin(Category, Category.id == RecurringSeries.category_id)
        .outerjoin(Account, Account.id == RecurringSeries.account_id)
        .where(RecurringSeries.status == status)
        .order_by(RecurringSeries.name, RecurringSeries.id)
    ).all()
    return [_series_row(series, category, account) for series, category, account in rows]


# Suggestions


@dataclass(frozen=True)
class SuggestionRow:
    suggestion: SeriesSuggestion
    account: str
    transactions: tuple[Transaction, ...]  # the charges the pattern was built from, newest first
    key: str = field(default_factory=lambda: uuid4().hex)  # for dismiss, made per render

    @property
    def name(self) -> str:
        return self.suggestion.name

    @property
    def cadence(self) -> str:
        return CADENCE_LABELS[self.suggestion.cadence.value]

    @property
    def amount_text(self) -> str:
        return format_cents(self.suggestion.amount_cents)

    @property
    def tolerance_text(self) -> str:
        return format_cents(self.suggestion.amount_tolerance_cents)

    @property
    def confidence_percent(self) -> int:
        return round(self.suggestion.confidence * 100)

    @property
    def identity(self) -> dict[str, str]:
        """The fields that name this suggestion. A confirm or dismiss posts them back, and the
        server finds the suggestion again by them."""
        return {
            "payee_pattern": self.suggestion.payee_pattern,
            "account_id": str(self.suggestion.account_id),
            "cadence": self.suggestion.cadence.value,
            "amount_cents": str(self.suggestion.amount_cents),
        }


def suggestion_rows(db: Session, today: date) -> list[SuggestionRow]:
    found = detect(db, today=today)
    accounts = dict(db.execute(select(Account.id, Account.name)).all())
    wanted = {txn_id for suggestion in found for txn_id in suggestion.occurrences}
    txns: dict[int, Transaction] = {}
    if wanted:
        txns = {
            txn.id: txn for txn in db.scalars(select(Transaction).where(Transaction.id.in_(wanted)))
        }
    rows: list[SuggestionRow] = []
    for suggestion in found:
        matched = sorted(
            (txns[txn_id] for txn_id in suggestion.occurrences if txn_id in txns),
            key=lambda txn: (txn.posted_on, txn.id),
            reverse=True,
        )
        rows.append(
            SuggestionRow(
                suggestion=suggestion,
                account=accounts.get(suggestion.account_id, "Unknown account"),
                transactions=tuple(matched),
            )
        )
    return rows


# Body, summary, and drawer


@dataclass(frozen=True)
class SummaryView:
    month: str
    summary: MonthSummary


def build_summary(db: Session, today: date) -> SummaryView:
    """This month's summary, whichever month the calendar shows."""
    start = first_of_month(today)
    occurrences = upcoming(db, start=start, end=month_end(start), today=today)
    return SummaryView(month=month_title(start), summary=summarize(occurrences))


@dataclass(frozen=True)
class BodyView:
    view: str
    month_start: date
    today: date
    upcoming: list[WeekGroup]
    calendar: list[list[CalendarDay]]
    confirmed: list[SeriesRow]
    dismissed: list[SeriesRow]

    @property
    def month_key(self) -> str:
        return month_key(self.month_start)

    @property
    def month_title(self) -> str:
        return month_title(self.month_start)

    @property
    def previous_key(self) -> str:
        return month_key(shift_month_start(self.month_start, -1))

    @property
    def next_key(self) -> str:
        return month_key(shift_month_start(self.month_start, 1))

    @property
    def is_current_month(self) -> bool:
        return self.month_start == first_of_month(self.today)


def build_body(db: Session, today: date, view: str, month_start: date) -> BodyView:
    weeks: list[WeekGroup] = []
    grid: list[list[CalendarDay]] = []
    confirmed: list[SeriesRow] = []
    dismissed: list[SeriesRow] = []
    if view == "upcoming":
        weeks = upcoming_weeks(
            [
                occurrence_view(item)
                for item in upcoming(
                    db, start=today, end=today + timedelta(days=UPCOMING_DAYS), today=today
                )
            ]
        )
    elif view == "calendar":
        start, end = calendar_range(month_start)
        grid = calendar_weeks(
            month_start,
            [occurrence_view(item) for item in upcoming(db, start=start, end=end, today=today)],
            today,
        )
    elif view == "all":
        confirmed = series_rows(db, SeriesStatus.confirmed)
    elif view == "history":
        dismissed = series_rows(db, SeriesStatus.dismissed)
    return BodyView(view, month_start, today, weeks, grid, confirmed, dismissed)


@dataclass(frozen=True)
class DrawerView:
    series: RecurringSeries
    account: str
    category: str
    matched: tuple[Transaction, ...]  # the last few charges, newest first
    matched_total: int
    occurrence: OccurrenceView | None  # the due date the drawer was opened from, if it is one
    link: str
    amount_text: str
    tolerance_text: str
    cadence: str
    next_due_text: str
    last_seen_text: str
    key: str = field(default_factory=lambda: uuid4().hex)  # for delete, made per render


def build_drawer(db: Session, series: RecurringSeries, today: date, due: date | None) -> DrawerView:
    account = db.get(Account, series.account_id) if series.account_id is not None else None
    category = db.get(Category, series.category_id) if series.category_id is not None else None
    matched = series_transactions(db, series, since=None)
    occurrence: OccurrenceView | None = None
    if due is not None:
        for item in upcoming(db, start=due, end=due, today=today):
            if item.series_id == series.id:
                occurrence = occurrence_view(item)
    row = _series_row(
        series,
        category.name if category else None,
        account.name if account else None,
    )
    return DrawerView(
        series=series,
        account=row.account,
        category=row.category,
        matched=tuple(reversed(matched[-DRAWER_TRANSACTIONS:])),
        matched_total=len(matched),
        occurrence=occurrence,
        link=row.link,
        amount_text=row.amount_text,
        tolerance_text=format_cents(series.amount_tolerance_cents),
        cadence=row.cadence,
        next_due_text=row.next_due_text,
        last_seen_text=row.last_seen_text,
    )
