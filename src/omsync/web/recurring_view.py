"""View models for the Recurring page: the Overview's totals, Needs attention, and lists, the
month grid, the All bills tables, the review drawer, the bill drawer, and the choices of the bill
form. Reads only. Writes are in routes/recurring.py.

A bill is negative, as in the ledger. The totals and the monthly costs are magnitudes, and the
Overview's totals say which occurrences they count. The interface says "bill", or "income" for an
inflow; `noun_for` gives that word. Each write a row or drawer offers is an ActionView: a POST with
the fields that name its charge or due date, and a key made per render, so a repeated click is one
change.
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
    AttentionItem,
    AttentionKind,
    MonthTotals,
    Occurrence,
    OccurrenceStatus,
    SeriesSuggestion,
    bills_per_month_cents,
    current_amount,
    first_per_series,
    is_transfer,
    last_seen,
    month_totals,
    monthly_equivalent_cents,
    needs_attention,
    next_due,
    price_history,
    review_queue,
    series_transactions,
    upcoming,
)
from omsync.web.formats import fmt_date, month_key, month_title, parse_month
from omsync.web.register_state import encode_filter

REVIEW_CHARGES = 4  # the matched charges a review shows as evidence
DRAWER_TRANSACTIONS = 6
CADENCE_LABELS: dict[str, str] = {
    Cadence.weekly.value: "Weekly",
    Cadence.biweekly.value: "Biweekly",
    Cadence.monthly.value: "Monthly",
    Cadence.quarterly.value: "Quarterly",
    Cadence.yearly.value: "Yearly",
}
# A chip's label and style, by what it says. A question (a new amount, a charge to check) and a mark
# paid elsewhere replace the plain state, which keeps its own label.
CHIP_LABELS: dict[str, str] = {
    "paid": "Paid",
    "upcoming": "Due",
    "missed": "Missed",
    "paid_elsewhere": "Paid elsewhere",
    "new_amount": "New amount?",
    "check": "Check",
}


# Dates, months, and words


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


def noun_for(amount_cents: int) -> str:
    """The word for a series in the interface: an inflow is income, anything else a bill."""
    return "income" if amount_cents > 0 else "bill"


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


def account_choices(db: Session) -> list[tuple[str, str]]:
    """(id, name) for the accounts a bill may belong to: the open ones, by name."""
    accounts = db.scalars(
        select(Account).where(Account.closed.is_(False)).order_by(Account.name)
    ).all()
    return [(str(account.id), account.name) for account in accounts]


# Actions


@dataclass(frozen=True)
class ActionView:
    """One write a row or drawer offers: a POST to url with its fields, and a key made per
    render."""

    label: str
    url: str
    values: dict[str, str]
    variant: str = "outline"
    key: str = field(default_factory=lambda: uuid4().hex)  # for the write, made per render


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
    flag: str | None = None  # the OccurrenceFlag value, when a question about the due date is open
    paid_elsewhere: bool = False
    is_transfer: bool = False

    @property
    def chip_kind(self) -> str:
        """The CHIP_LABELS key for this occurrence. An open question wins over the plain state."""
        if self.flag == "new_amount":
            return "new_amount"
        if self.flag == "ambiguous":
            return "check"
        if self.paid_elsewhere:
            return "paid_elsewhere"
        return self.state

    @property
    def chip_label(self) -> str:
        return CHIP_LABELS[self.chip_kind]

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
        flag=occurrence.flag.value if occurrence.flag is not None else None,
        paid_elsewhere=occurrence.paid_elsewhere,
        is_transfer=occurrence.is_transfer,
    )


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


# Series rows (All bills)


@dataclass(frozen=True)
class SeriesRow:
    id: int
    name: str
    cadence: str
    amount_text: str
    amount_cents: int
    monthly_cents: int  # the monthly equivalent, signed like the amount
    monthly_text: str | None  # None for a monthly bill, which needs no equivalent shown
    tolerance_cents: int
    next_due_text: str
    last_seen_text: str
    ended_text: str  # the day an ended bill stopped, or empty
    category: str
    account: str
    link: str
    transfer: bool  # left out of totals, so the row says so
    key: str = field(default_factory=lambda: uuid4().hex)  # for restore, made per render


def _series_row(
    series: RecurringSeries,
    category: str | None,
    account: str | None,
    *,
    amount_cents: int,
    next_due_on: date | None,
    last_seen_on: date | None,
    transfer: bool,
) -> SeriesRow:
    tolerance = (
        f" ± {format_cents(series.amount_tolerance_cents)}" if series.amount_tolerance_cents else ""
    )
    monthly = monthly_equivalent_cents(amount_cents, series.cadence)
    return SeriesRow(
        id=series.id,
        name=series.name,
        cadence=CADENCE_LABELS[series.cadence.value],
        amount_text=f"{format_cents(amount_cents)}{tolerance}",
        amount_cents=amount_cents,
        monthly_cents=monthly,
        monthly_text=None if series.cadence is Cadence.monthly else format_cents(monthly),
        tolerance_cents=series.amount_tolerance_cents,
        next_due_text=fmt_date(next_due_on) if next_due_on else "Not set",
        last_seen_text=fmt_date(last_seen_on) if last_seen_on else "Never",
        ended_text=fmt_date(series.ended_on) if series.ended_on else "",
        category=category or "Uncategorized",
        account=account or "Any account",
        link=transactions_href(series.payee_pattern, series.account_id),
        transfer=transfer,
    )


def series_rows(db: Session, status: SeriesStatus, today: date) -> list[SeriesRow]:
    rows = db.execute(
        select(RecurringSeries, Category.name, Account.name)
        .outerjoin(Category, Category.id == RecurringSeries.category_id)
        .outerjoin(Account, Account.id == RecurringSeries.account_id)
        .where(RecurringSeries.status == status)
        .order_by(RecurringSeries.name, RecurringSeries.id)
    ).all()
    return [
        _series_row(
            series,
            category,
            account,
            amount_cents=current_amount(db, series),
            next_due_on=next_due(db, series, today=today)
            if status is SeriesStatus.confirmed
            else None,
            last_seen_on=last_seen(db, series, today=today),
            transfer=is_transfer(db, series),
        )
        for series, category, account in rows
    ]


# Overview


@dataclass(frozen=True)
class AttentionRowView:
    """One Needs attention item. The text opens the bill's drawer. `actions` answer the item's
    question; `charges` lists the candidates of an ambiguous pair, each with its own Not this
    bill."""

    kind: str  # the AttentionKind value
    series_id: int
    text: str
    drawer_href: str
    actions: tuple[ActionView, ...] = ()
    charges: tuple["ChargeView", ...] = ()


@dataclass(frozen=True)
class ChargeView:
    text: str
    action: ActionView


@dataclass(frozen=True)
class OverviewView:
    month: str  # the month the totals and the This month list are for
    totals: MonthTotals
    review_count: int  # possible bills waiting for review, not counted yet
    attention: tuple[AttentionRowView, ...]  # this month's open questions, by day
    this_month: tuple[OccurrenceView, ...]  # this month's occurrences, except the missed ones
    next_month: tuple[OccurrenceView, ...]  # each series' first occurrence next month
    empty: bool  # no confirmed bill, nothing to review, and nothing to attend to


def build_overview(db: Session, today: date) -> OverviewView:
    start = first_of_month(today)
    following = shift_month_start(start, 1)
    this_month = upcoming(db, start=start, end=month_end(start), today=today)
    next_month = first_per_series(
        upcoming(db, start=following, end=month_end(following), today=today)
    )
    review_count = len(review_queue(db, today=today))
    attention = attention_rows(db, needs_attention(db, today=today))
    has_bills = (
        db.scalar(
            select(RecurringSeries.id)
            .where(RecurringSeries.status == SeriesStatus.confirmed)
            .limit(1)
        )
        is not None
    )
    return OverviewView(
        month=month_title(start),
        totals=month_totals(this_month),
        review_count=review_count,
        attention=attention,
        this_month=tuple(
            occurrence_view(item)
            for item in this_month
            if item.status is not OccurrenceStatus.missed
        ),
        next_month=tuple(occurrence_view(item) for item in next_month),
        empty=not has_bills and review_count == 0 and not attention,
    )


def attention_rows(db: Session, items: list[AttentionItem]) -> tuple[AttentionRowView, ...]:
    return tuple(_attention_row(db, item) for item in items)


def _attention_row(db: Session, item: AttentionItem) -> AttentionRowView:
    base = f"/recurring/series/{item.series_id}"
    series = db.get(RecurringSeries, item.series_id)
    can_end = series is not None and series.status is SeriesStatus.confirmed
    noun = noun_for(item.expected_cents)
    due = short_day(item.day)
    if item.kind is AttentionKind.missed:
        actions = [
            ActionView("Paid elsewhere", f"{base}/paid-elsewhere", {"due_on": item.day.isoformat()})
        ]
        if can_end:
            # Ending the day before the due date drops this date and every later one.
            ended = (item.day - timedelta(days=1)).isoformat()
            actions.append(ActionView(f"End {noun}", f"{base}/end", {"ended_on": ended}))
        return AttentionRowView(
            kind=item.kind.value,
            series_id=item.series_id,
            text=(
                f"{item.series_name} — due {due}, {format_cents(item.expected_cents)}, "
                "no charge found."
            ),
            drawer_href=f"{base}/drawer?due={item.day.isoformat()}",
            actions=tuple(actions),
        )
    if item.kind is AttentionKind.ambiguous:
        candidates = tuple(
            tid for tid in (item.transaction_id, *item.other_transaction_ids) if tid is not None
        )
        found = {
            txn.id: txn
            for txn in db.scalars(select(Transaction).where(Transaction.id.in_(candidates)))
        }
        charges = tuple(
            ChargeView(
                text=(
                    f"{short_day(found[tid].posted_on)} · {found[tid].payee} · "
                    f"{format_cents(found[tid].amount_cents)}"
                ),
                action=ActionView("Not this bill", f"{base}/reject", {"transaction_id": str(tid)}),
            )
            for tid in candidates
            if tid in found
        )
        return AttentionRowView(
            kind=item.kind.value,
            series_id=item.series_id,
            text=f"{item.series_name} — more than one charge could pay {due}.",
            drawer_href=f"{base}/drawer?due={item.day.isoformat()}",
            charges=charges,
        )
    charge = str(item.transaction_id)
    if item.kind is AttentionKind.new_amount:
        return AttentionRowView(
            kind=item.kind.value,
            series_id=item.series_id,
            text=(
                f"{item.series_name} charged {format_cents(item.actual_cents or 0)}, "
                f"expected {format_cents(item.expected_cents)}."
            ),
            drawer_href=f"{base}/drawer?due={item.day.isoformat()}",
            actions=(
                ActionView(
                    "Use new amount",
                    f"{base}/accept-amount",
                    {"transaction_id": charge},
                    variant="primary",
                ),
                ActionView("Not this bill", f"{base}/reject", {"transaction_id": charge}),
            ),
        )
    return AttentionRowView(
        kind=item.kind.value,
        series_id=item.series_id,
        text=(
            f"{item.series_name} charged {format_cents(item.actual_cents or 0)} on "
            f"{short_day(item.day)}, after you ended it."
        ),
        drawer_href=f"{base}/drawer",
        actions=(
            ActionView("Resume", f"{base}/resume", {}, variant="primary"),
            ActionView("Not this bill", f"{base}/reject", {"transaction_id": charge}),
        ),
    )


# Review


@dataclass(frozen=True)
class ReviewView:
    """One possible bill in the review drawer, at its place in the queue. Add bill and Not recurring
    each post the identity back with their own key."""

    index: int  # the place in the queue, from 0
    count: int  # how many suggestions are waiting, this one included
    suggestion: SeriesSuggestion
    account: str
    transactions: tuple[Transaction, ...]  # the last few charges, newest first
    confirm_key: str = field(default_factory=lambda: uuid4().hex)  # made per render
    dismiss_key: str = field(default_factory=lambda: uuid4().hex)  # made per render

    @property
    def position(self) -> str:
        return f"{self.index + 1} of {self.count}"

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
    def identity(self) -> dict[str, str]:
        """The fields that name this suggestion. A decision posts them back, and the server finds
        the suggestion again by them."""
        return {
            "payee_pattern": self.suggestion.payee_pattern,
            "account_id": str(self.suggestion.account_id),
            "cadence": self.suggestion.cadence.value,
            "amount_cents": str(self.suggestion.amount_cents),
        }


def review_view(db: Session, today: date, index: int) -> ReviewView | None:
    """The suggestion at `index` in the review queue. None when there is no such place: the queue
    is empty, or the review has moved past its end."""
    queue = review_queue(db, today=today)
    if not 0 <= index < len(queue):
        return None
    suggestion = queue[index]
    account = db.get(Account, suggestion.account_id)
    transactions = db.scalars(
        select(Transaction)
        .where(Transaction.id.in_(suggestion.occurrences))
        .order_by(Transaction.posted_on.desc(), Transaction.id.desc())
        .limit(REVIEW_CHARGES)
    ).all()
    return ReviewView(
        index=index,
        count=len(queue),
        suggestion=suggestion,
        account=account.name if account is not None else "Unknown account",
        transactions=tuple(transactions),
    )


# Body


@dataclass(frozen=True)
class BodyView:
    view: str  # overview, calendar, or all
    status: str  # the All bills filter: active, ended, or dismissed
    month_start: date
    today: date
    overview: OverviewView | None
    calendar: list[list[CalendarDay]]
    series: list[SeriesRow]  # the bills the All bills filter shows
    monthly_cents: int  # the outflow bills per month, for the active footer

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

    @property
    def yearly_cents(self) -> int:
        return self.monthly_cents * 12


def build_body(db: Session, today: date, view: str, month_start: date, status: str) -> BodyView:
    overview: OverviewView | None = None
    grid: list[list[CalendarDay]] = []
    rows: list[SeriesRow] = []
    monthly = 0
    if view == "overview":
        overview = build_overview(db, today)
    elif view == "calendar":
        start, end = calendar_range(month_start)
        grid = calendar_weeks(
            month_start,
            [occurrence_view(item) for item in upcoming(db, start=start, end=end, today=today)],
            today,
        )
    elif status == "dismissed":
        rows = series_rows(db, SeriesStatus.dismissed, today)
    elif status == "ended":
        rows = series_rows(db, SeriesStatus.ended, today)
    else:
        rows = sorted(
            series_rows(db, SeriesStatus.confirmed, today),
            key=lambda row: (-abs(row.monthly_cents), row.name.casefold(), row.id),
        )
        monthly = bills_per_month_cents(
            db,
            db.scalars(
                select(RecurringSeries).where(RecurringSeries.status == SeriesStatus.confirmed)
            ),
        )
    return BodyView(view, status, month_start, today, overview, grid, rows, monthly)


# Drawers


@dataclass(frozen=True)
class DrawerView:
    series: RecurringSeries
    noun: str  # "bill" or "income"
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
    ended_text: str
    history: tuple[tuple[str, str], ...]  # (when, amount), oldest first; empty without price rows
    actions: tuple[ActionView, ...]
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
    current = current_amount(db, series)
    row = _series_row(
        series,
        category.name if category else None,
        account.name if account else None,
        amount_cents=current,
        next_due_on=(
            next_due(db, series, today=today) if series.status is SeriesStatus.confirmed else None
        ),
        last_seen_on=last_seen(db, series, today=today),
        transfer=is_transfer(db, series),
    )
    history = price_history(db, series)
    return DrawerView(
        series=series,
        noun=noun_for(series.amount_cents),
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
        ended_text=row.ended_text,
        history=(
            tuple(
                (
                    "Starting amount" if day is None else f"From {fmt_date(day)}",
                    format_cents(cents),
                )
                for day, cents in history
            )
            if len(history) > 1
            else ()
        ),
        actions=_drawer_actions(series, occurrence, today),
    )


def _drawer_actions(
    series: RecurringSeries, occurrence: OccurrenceView | None, today: date
) -> tuple[ActionView, ...]:
    """The drawer's writes. Each carries from_drawer, so its reply reloads the drawer."""
    base = f"/recurring/series/{series.id}"
    noun = noun_for(series.amount_cents)
    actions: list[ActionView] = []
    if occurrence is not None and occurrence.state in ("upcoming", "missed"):
        actions.append(
            ActionView(
                "Paid elsewhere",
                f"{base}/paid-elsewhere",
                {"due_on": occurrence.due_on.isoformat(), "from_drawer": "1"},
            )
        )
    if series.status is SeriesStatus.confirmed:
        actions.append(
            ActionView(
                f"End {noun}",
                f"{base}/end",
                {"ended_on": today.isoformat(), "from_drawer": "1"},
            )
        )
    if series.status is SeriesStatus.ended:
        actions.append(
            ActionView("Resume", f"{base}/resume", {"from_drawer": "1"}, variant="primary")
        )
    return tuple(actions)
