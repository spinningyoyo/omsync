"""Report queries: income, expenses, net, savings rate, spending by category, and monthly trends.

Every money value is integer cents. Rates and shares are integers in basis points (1/100 of a
percent), so no float reaches the data path. These are reads: nothing here writes.

A line is what a report counts. A transaction with splits contributes its splits, and any other
transaction contributes itself. Lines never count as income or spending when:

- a line whose category is excluded;
- an uncategorized line. uncategorized_total reports those separately.

A transfer or joint_settlement link explains part of its transactions' amounts. The explained part
never counts, so a transaction counts only the amount its links leave unexplained. A transaction
that its links explain in full contributes nothing.

With joint share on, a transaction on a joint account counts at half, once: allocate(abs(amount),
[1, 1])[0] of the counted amount, with the sign kept. An odd cent goes to the first share. Its
splits then share that counted amount in proportion to their own amounts, so splitting a
transaction never changes what it counts. The shares are exact: they add up to the counted amount.

Income is the sum of all lines in income categories, so a negative income line (a reversal, or a tax
payment filed under an income category) reduces it. Expenses are the negated sum of lines in other
categories, so spending is positive and a refund (a positive line in a spending category) reduces
it. Net is income less expenses. The savings rate is net over income, and None when income is 0.

Periods are inclusive on both ends. This module never imports omsync.web.
"""

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

from sqlalchemy import Row, or_, select
from sqlalchemy.orm import Session

from omsync.core.money import allocate
from omsync.core.months import month_end, shift_month_start
from omsync.ledger.models import (
    Account,
    Category,
    CategoryType,
    Link,
    LinkKind,
    Split,
    Transaction,
)

PRESETS: tuple[str, ...] = ("this_month", "last_month", "this_quarter", "ytd", "last_12_months")
EXCLUDED_LINK_KINDS: tuple[LinkKind, ...] = (LinkKind.transfer, LinkKind.joint_settlement)
SPARK_MONTHS = 6
TREND_MONTHS = 12
BASIS_POINTS = 10_000


@dataclass(frozen=True)
class Period:
    """A range of dates, both ends inclusive."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("a period cannot end before it starts")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> "Period":
        """The period of the same length that ends the day before this one starts."""
        end = self.start - timedelta(days=1)
        return Period(end - timedelta(days=self.days - 1), end)

    @classmethod
    def preset(cls, key: str, today: date) -> "Period":
        """A named period relative to today. Raises ValueError for an unknown key.

        this_month, this_quarter, ytd, and last_12_months end today, so they compare with a
        same-length period before them. last_month is the whole previous calendar month.
        last_12_months starts on the first day of the month eleven months back, so it covers the
        same twelve months as monthly_trend.
        """
        if key == "this_month":
            return cls(today.replace(day=1), today)
        if key == "last_month":
            last_day = today.replace(day=1) - timedelta(days=1)
            return cls(last_day.replace(day=1), last_day)
        if key == "this_quarter":
            first_month = 3 * ((today.month - 1) // 3) + 1
            return cls(date(today.year, first_month, 1), today)
        if key == "ytd":
            return cls(date(today.year, 1, 1), today)
        if key == "last_12_months":
            return cls(shift_month_start(today, -11), today)
        raise ValueError(f"unknown period {key!r}")


@dataclass(frozen=True)
class Figures:
    income_cents: int
    expenses_cents: int  # spending, as a positive number
    net_cents: int
    savings_rate_bp: int | None  # net over income; None when income is 0


@dataclass(frozen=True)
class Change:
    """How a figure moved against the previous period. pct_bp is None when that was 0."""

    delta_cents: int
    pct_bp: int | None


@dataclass(frozen=True)
class Summary:
    period: Period
    prior_period: Period
    joint_share: bool
    figures: Figures
    prior: Figures
    income_change: Change
    expenses_change: Change
    net_change: Change
    savings_rate_delta_bp: int | None  # None when either period has no income


@dataclass(frozen=True)
class CategoryLine:
    """One spending category in a period. Amounts are spending, so refunds reduce them."""

    category_id: int
    name: str
    type: CategoryType
    amount_cents: int
    share_bp: int | None  # of the period's expenses; None when expenses are 0
    prior_cents: int
    change: Change
    monthly_cents: tuple[
        int, ...
    ]  # SPARK_MONTHS months, oldest first, ending in the period's month


@dataclass(frozen=True)
class MonthPoint:
    month: date  # the first day of the month
    income_cents: int
    expenses_cents: int
    net_cents: int


@dataclass(frozen=True)
class ReportLine:
    transaction_id: int
    posted_on: date
    amount_cents: int  # after explained amounts and joint share
    category_id: int | None
    category_name: str | None
    type: CategoryType | None  # None for an uncategorized line

    @property
    def is_income(self) -> bool:
        return self.type is CategoryType.income


# Public queries


def summary(session: Session, period: Period, *, joint_share: bool) -> Summary:
    """Income, expenses, net, and savings rate for the period, against the previous period."""
    prior_period = period.previous()
    figures = _figures(report_lines(session, period, joint_share=joint_share))
    prior = _figures(report_lines(session, prior_period, joint_share=joint_share))
    savings_delta = None
    if figures.savings_rate_bp is not None and prior.savings_rate_bp is not None:
        savings_delta = figures.savings_rate_bp - prior.savings_rate_bp
    return Summary(
        period=period,
        prior_period=prior_period,
        joint_share=joint_share,
        figures=figures,
        prior=prior,
        income_change=_change(figures.income_cents, prior.income_cents),
        expenses_change=_change(figures.expenses_cents, prior.expenses_cents),
        net_change=_change(figures.net_cents, prior.net_cents),
        savings_rate_delta_bp=savings_delta,
    )


def by_category(session: Session, period: Period, *, joint_share: bool) -> list[CategoryLine]:
    """Spending by category for the period, largest first. Categories with spending in either the
    period or the previous one are listed. Their amounts add up to the period's expenses."""
    prior_period = period.previous()
    months = _month_starts(period.end, SPARK_MONTHS)
    current = report_lines(session, period, joint_share=joint_share)
    prior = report_lines(session, prior_period, joint_share=joint_share)
    recent = report_lines(
        session, Period(months[0], month_end(months[-1])), joint_share=joint_share
    )

    spent = _spending_by_category(current)
    before = _spending_by_category(prior)
    names = _category_names(current + prior + recent)
    by_month = _spending_by_category_month(recent)
    total = sum(spent.values())

    rows = []
    for category_id in set(spent) | set(before):
        amount = spent.get(category_id, 0)
        prior_amount = before.get(category_id, 0)
        name, category_type = names[category_id]
        rows.append(
            CategoryLine(
                category_id=category_id,
                name=name,
                type=category_type,
                amount_cents=amount,
                share_bp=ratio_bp(amount, total),
                prior_cents=prior_amount,
                change=_change(amount, prior_amount),
                monthly_cents=tuple(
                    by_month.get((category_id, (month.year, month.month)), 0) for month in months
                ),
            )
        )
    return sorted(rows, key=lambda row: (-row.amount_cents, row.name.casefold(), row.category_id))


def monthly_trend(
    session: Session, months: int = TREND_MONTHS, *, end: date, joint_share: bool
) -> list[MonthPoint]:
    """Income, expenses, and net for each of the last `months` calendar months, oldest first.
    The last month is the one that contains `end`, and each month is counted in full."""
    if months < 1:
        raise ValueError("a trend needs at least one month")
    starts = _month_starts(end, months)
    lines = report_lines(session, Period(starts[0], month_end(starts[-1])), joint_share=joint_share)
    by_month: dict[tuple[int, int], list[ReportLine]] = defaultdict(list)
    for line in lines:
        by_month[(line.posted_on.year, line.posted_on.month)].append(line)
    points = []
    for start in starts:
        figures = _figures(by_month.get((start.year, start.month), []))
        points.append(
            MonthPoint(
                month=start,
                income_cents=figures.income_cents,
                expenses_cents=figures.expenses_cents,
                net_cents=figures.net_cents,
            )
        )
    return points


def spending_by_month(
    session: Session, start: date, end: date, *, joint_share: bool
) -> dict[date, dict[int, int]]:
    """Spending per category for each calendar month that starts in start through end, keyed by the
    month's first day. The same lines as by_category, read in one query, so a caller that needs
    many months (budget rollover, for one) does not rebuild the report once per month."""
    spent: dict[date, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for line in report_lines(session, Period(start, end), joint_share=joint_share):
        if line.category_id is not None and not line.is_income:
            month = line.posted_on.replace(day=1)
            spent[month][line.category_id] -= line.amount_cents
    return {month: dict(by_category) for month, by_category in spent.items()}


def uncategorized_total(session: Session, period: Period, *, joint_share: bool) -> int:
    """The net outflow of the period's uncategorized lines, as a positive number like expenses.
    Uncategorized inflows reduce it. These lines are not in income or expenses."""
    lines = report_lines(session, period, joint_share=joint_share)
    return -sum(line.amount_cents for line in lines if line.category_id is None)


def has_transactions(session: Session) -> bool:
    """Whether any transaction exists. The reports page shows its empty state when none does."""
    return session.scalar(select(Transaction.id).limit(1)) is not None


# Lines


def report_lines(session: Session, period: Period, *, joint_share: bool) -> list[ReportLine]:
    """Every counted line posted in the period, with explained amounts and joint share applied.
    Lines of an excluded category are dropped here, so every figure shares one definition of a
    line."""
    explained = explained_cents(session, period)
    split_rows = session.execute(
        select(
            Split.transaction_id,
            Transaction.posted_on,
            Split.amount_cents,
            Split.category_id,
            Category.name,
            Category.type,
        )
        .select_from(Split)
        .join(Transaction, Transaction.id == Split.transaction_id)
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Split.category_id)
        .where(Transaction.posted_on >= period.start, Transaction.posted_on <= period.end)
    ).all()
    txn_rows = session.execute(
        select(
            Transaction.id,
            Transaction.posted_on,
            Transaction.amount_cents,
            Transaction.category_id,
            Category.name,
            Category.type,
            Account.joint,
        )
        .select_from(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(Transaction.posted_on >= period.start, Transaction.posted_on <= period.end)
    ).all()

    splits_of: dict[int, list[Row]] = defaultdict(list)
    for split in split_rows:
        splits_of[split.transaction_id].append(split)
    lines: list[ReportLine] = []
    for row in txn_rows:
        halve = joint_share and bool(row.joint)
        covered = explained.get(row.id, 0)
        counted = counted_amount(row.amount_cents, covered, halve)
        if covered and counted == 0:
            continue  # its links explain the whole transaction
        parts = splits_of.get(row.id)
        if parts is None:
            lines.append(_line(row.id, row, counted))
            continue
        shares = counted_splits(row.amount_cents, [part.amount_cents for part in parts], counted)
        lines += [_line(row.id, part, share) for part, share in zip(parts, shares, strict=True)]
    return [line for line in lines if _counts(line)]


def _line(transaction_id: int, row: Row, amount: int) -> ReportLine:
    """One counted line. The amount is already counted: explained, halved, and shared out."""
    return ReportLine(
        transaction_id=transaction_id,
        posted_on=row.posted_on,
        amount_cents=amount,
        category_id=row.category_id,
        category_name=row.name,
        type=row.type,
    )


def _counts(line: ReportLine) -> bool:
    """False for a line whose category is excluded from reports."""
    return line.type is not CategoryType.excluded


def explained_cents(session: Session, period: Period) -> dict[int, int]:
    """For each transaction posted in the period, the cents that transfer and joint settlement
    links explain. A link's amount is a magnitude, and it explains that much on each of its
    sides."""
    in_period = select(Transaction.id).where(
        Transaction.posted_on >= period.start, Transaction.posted_on <= period.end
    )
    rows = session.execute(
        select(Link.from_transaction_id, Link.to_transaction_id, Link.amount_cents).where(
            Link.kind.in_(EXCLUDED_LINK_KINDS),
            or_(Link.from_transaction_id.in_(in_period), Link.to_transaction_id.in_(in_period)),
        )
    ).all()
    totals: dict[int, int] = defaultdict(int)
    for from_id, to_id, amount in rows:
        for txn_id in {from_id, to_id}:
            if txn_id is not None:
                totals[txn_id] += amount
    return dict(totals)


def counted_amount(amount: int, covered: int, halve: bool) -> int:
    """What a transaction counts, with its sign: the part its links leave unexplained, then half of
    that for a joint account when joint share is on. The half is taken once per transaction, so
    splitting the transaction never changes it. An odd cent goes to the first share."""
    magnitude = max(abs(amount) - covered, 0)
    if halve:
        magnitude = allocate(magnitude, [1, 1])[0]
    return magnitude if amount >= 0 else -magnitude


def counted_splits(amount: int, parts: Sequence[int], counted: int) -> list[int]:
    """Each split's share of its transaction's counted amount, in proportion to the split's own
    amount, so the shares add up to counted exactly. A transaction counted in full keeps its splits
    as they are."""
    if counted == amount:
        return list(parts)
    whole = sum(parts)
    if whole == 0:
        return [0] * len(parts)  # splits that do not add up to their transaction have no shares
    return _apportion(counted, parts, whole)


def _apportion(total: int, parts: Sequence[int], whole: int) -> list[int]:
    """Split total in proportion to parts, which add up to whole. Shares are floored, and the
    leftover cents go to the largest fractions, earlier index first. The shares sum to total."""
    exact = [Fraction(total * part, whole) for part in parts]
    shares = [math.floor(value) for value in exact]
    leftover = total - sum(shares)  # a whole number from zero up to the count of parts
    by_fraction = sorted(range(len(parts)), key=lambda i: (-(exact[i] - shares[i]), i))
    for index in by_fraction[:leftover]:
        shares[index] += 1
    return shares


# Aggregation


def _figures(lines: Sequence[ReportLine]) -> Figures:
    income = 0
    expenses = 0
    for line in lines:
        if line.category_id is None:
            continue
        if line.is_income:
            income += line.amount_cents
        else:
            expenses -= line.amount_cents
    net = income - expenses
    return Figures(
        income_cents=income,
        expenses_cents=expenses,
        net_cents=net,
        savings_rate_bp=ratio_bp(net, income),
    )


def _spending_by_category(lines: Sequence[ReportLine]) -> dict[int, int]:
    """Spending per category, positive, with refunds subtracted. Income categories are left out."""
    spent: dict[int, int] = defaultdict(int)
    for line in lines:
        if line.category_id is not None and not line.is_income:
            spent[line.category_id] -= line.amount_cents
    return dict(spent)


def _spending_by_category_month(
    lines: Sequence[ReportLine],
) -> dict[tuple[int, tuple[int, int]], int]:
    spent: dict[tuple[int, tuple[int, int]], int] = defaultdict(int)
    for line in lines:
        if line.category_id is not None and not line.is_income:
            key = (line.category_id, (line.posted_on.year, line.posted_on.month))
            spent[key] -= line.amount_cents
    return dict(spent)


def _category_names(lines: Sequence[ReportLine]) -> dict[int, tuple[str, CategoryType]]:
    names: dict[int, tuple[str, CategoryType]] = {}
    for line in lines:
        if line.category_id is not None and line.category_name is not None and line.type:
            names[line.category_id] = (line.category_name, line.type)
    return names


def _change(current: int, prior: int) -> Change:
    delta = current - prior
    return Change(delta_cents=delta, pct_bp=ratio_bp(delta, abs(prior)))


# Dates and rates


def _month_starts(end: date, count: int) -> list[date]:
    """The first days of `count` months, oldest first, ending with the month that contains `end`."""
    last = end.replace(day=1)
    return [shift_month_start(last, -(count - 1 - index)) for index in range(count)]


def ratio_bp(numerator: int, denominator: int) -> int | None:
    """numerator / denominator in basis points, rounded half away from zero. None for 0."""
    if denominator == 0:
        return None
    sign = -1 if (numerator < 0) != (denominator < 0) else 1
    top, bottom = abs(numerator), abs(denominator)
    return sign * ((2 * top * BASIS_POINTS + bottom) // (2 * bottom))


__all__ = [
    "ReportLine",
    "report_lines",
    "PRESETS",
    "CategoryLine",
    "Change",
    "Figures",
    "MonthPoint",
    "Period",
    "Summary",
    "by_category",
    "counted_amount",
    "counted_splits",
    "explained_cents",
    "has_transactions",
    "monthly_trend",
    "ratio_bp",
    "spending_by_month",
    "summary",
    "uncategorized_total",
]
