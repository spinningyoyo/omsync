"""Monthly budget views and changes. Month arguments use the first day of the month."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import Change, applied_batch_for_key, apply, make_preview, snapshot
from omsync.core.months import month_end, shift_month_start
from omsync.ledger.categories import (
    DEFAULT_CATEGORY_ICON,
    NOT_IN_REPORTS,
    Category,
    CategoryType,
)
from omsync.ledger.preferences import get_pref
from omsync.ledger.spending import Period, by_category, monthly_trend, spending_by_month
from omsync.plan.models import BudgetKind, BudgetLine, Goal

BUDGET_MODE_KEY = "budget_mode"
EXPECTED_INCOME_KEY = "expected_income_cents"
EXPECTED_INCOME_MONTHS = 3
BASIS_POINTS = 10_000
WARNING_BP = 5_000  # 50%: progress from here is warning
OVER_BP = 10_000  # 100%: progress from here is over


class BudgetMode(StrEnum):
    flex = "flex"
    category = "category"


class ProgressState(StrEnum):
    good = "good"  # under 50%
    warning = "warning"  # 50% up to 100%, not reached
    over = "over"  # 100% or more


@dataclass(frozen=True)
class Carry:
    """What a category carries into a month: the budget of every earlier month with rollover on,
    the spending of those months, and the first of them. The carry is budgeted - spent."""

    budgeted_cents: int
    spent_cents: int
    since: date

    @property
    def cents(self) -> int:
        return self.budgeted_cents - self.spent_cents


@dataclass(frozen=True)
class BudgetRow:
    """One category in a month. available = budget + carry - actual. Progress is actual over
    (budget + carry), so it agrees with available. It is None when there is nothing to spend.
    `carry` is None when no earlier month rolled over."""

    category_id: int
    name: str
    type: CategoryType
    icon: str
    budget_cents: int
    actual_cents: int
    carry: Carry | None
    rollover: bool
    available_cents: int
    progress_bp: int | None
    progress_state: ProgressState | None

    @property
    def carry_cents(self) -> int:
        return 0 if self.carry is None else self.carry.cents


@dataclass(frozen=True)
class SpendRow:
    """A spending category with no budget in the month, and its actual."""

    category_id: int
    name: str
    actual_cents: int


@dataclass(frozen=True)
class BucketTotals:
    """One flex bucket's month. remaining = budget + carry - spent. Only non-monthly has a carry.
    Progress is spent over (budget + carry), as for a row."""

    budget_cents: int
    carry_cents: int
    spent_cents: int
    remaining_cents: int
    progress_bp: int | None
    progress_state: ProgressState | None


@dataclass(frozen=True)
class FlexView:
    """A month in flex mode. Fixed and non-monthly rows make up the flex number's deductions.
    Flexible rows show actuals only, and the flex number is the flexible bucket's budget.
    Unassigned rows are outside the flex number. `pace_bp` is how far through the month `today`
    is, in basis points, and None unless `month` is the current month."""

    month: date
    joint_share: bool
    expected_income_cents: int
    expected_income_is_override: bool
    fixed: tuple[BudgetRow, ...]
    non_monthly: tuple[BudgetRow, ...]
    flexible: tuple[BudgetRow, ...]
    unassigned: tuple[BudgetRow, ...]
    fixed_total_cents: int
    non_monthly_total_cents: int
    goals_contribution_cents: int
    flex_number_cents: int
    flex_number_is_override: bool
    flexible_actual_cents: int
    remaining_cents: int
    progress_bp: int | None
    progress_state: ProgressState | None
    fixed_bucket: BucketTotals
    non_monthly_bucket: BucketTotals
    flexible_bucket: BucketTotals
    unassigned_spent_cents: int
    pace_bp: int | None


@dataclass(frozen=True)
class CategoryView:
    """A month in category mode: each budgeted category, then the spending that has no budget."""

    month: date
    joint_share: bool
    rows: tuple[BudgetRow, ...]
    unbudgeted: tuple[SpendRow, ...]
    budget_total_cents: int
    carry_total_cents: int
    actual_total_cents: int
    available_total_cents: int
    unbudgeted_total_cents: int
    progress_bp: int | None
    progress_state: ProgressState | None


# Reads


def get_mode(session: Session) -> BudgetMode:
    """The budget mode. Flex unless you chose category."""
    return BudgetMode(get_pref(session, BUDGET_MODE_KEY, BudgetMode.flex.value))


def expected_income_override(session: Session) -> int | None:
    """The expected income you typed in, or None when it is worked out from past months."""
    value = get_pref(session, EXPECTED_INCOME_KEY, None)
    return None if value is None else int(value)


def actual_by_category(session: Session, month: date, *, joint_share: bool) -> dict[int, int]:
    """Net spending per category for the month, positive. Refunds reduce it, and income
    categories and uncategorized lines are left out, exactly as in reports."""
    _check_month(month)
    rows = by_category(session, _month_period(month), joint_share=joint_share)
    return {row.category_id: row.amount_cents for row in rows}


def expected_income(session: Session, month: date, *, joint_share: bool) -> int:
    """The override when one is set. Otherwise the average monthly income of the three
    months before `month`, from reports, in cents with floor division. A month with no
    income counts as 0."""
    _check_month(month)
    override = expected_income_override(session)
    if override is not None:
        return override
    points = monthly_trend(
        session,
        EXPECTED_INCOME_MONTHS,
        end=month - timedelta(days=1),
        joint_share=joint_share,
    )
    return sum(point.income_cents for point in points) // EXPECTED_INCOME_MONTHS


def goals_contribution_cents(session: Session) -> int:
    """The monthly contributions of all goals that are not archived."""
    total = session.scalar(
        select(func.coalesce(func.sum(Goal.monthly_contribution_cents), 0)).where(
            Goal.archived.is_(False)
        )
    )
    return int(total or 0)


def flex_view(session: Session, month: date, *, joint_share: bool, today: date) -> FlexView:
    """Flex mode for one month. Rollover carry counts only for non-monthly categories."""
    _check_month(month)
    categories = _categories(session)
    lines = _category_lines(session, month)
    pool = _flex_pool(session, month)
    actual = actual_by_category(session, month, joint_share=joint_share)
    carry = _carries(session, month, joint_share=joint_share)

    buckets: dict[CategoryType, list[BudgetRow]] = {
        category_type: [] for category_type in CategoryType
    }
    for category in categories.values():
        line = lines.get(category.id)
        spent = actual.get(category.id, 0)
        carried = carry.get(category.id) if category.type is CategoryType.non_monthly else None
        if line is None and spent == 0 and (carried is None or carried.cents == 0):
            continue
        buckets[category.type].append(_row(category, line, spent, carried))

    fixed = _sorted(buckets[CategoryType.fixed])
    non_monthly = _sorted(buckets[CategoryType.non_monthly])
    flexible = _sorted(buckets[CategoryType.flexible])
    unassigned = _sorted(buckets[CategoryType.unassigned])

    fixed_total = sum(row.budget_cents for row in fixed)
    non_monthly_total = sum(row.budget_cents for row in non_monthly)
    goals = goals_contribution_cents(session)
    income = expected_income(session, month, joint_share=joint_share)
    worked_out = income - fixed_total - non_monthly_total - goals
    flex_number = worked_out if pool is None else pool
    flexible_ids = {
        category.id for category in categories.values() if category.type is CategoryType.flexible
    }
    flexible_actual = sum(
        spent for category_id, spent in actual.items() if category_id in flexible_ids
    )
    basis, state = progress(flex_number, 0, flexible_actual)
    days = month_end(month).day
    return FlexView(
        month=month,
        joint_share=joint_share,
        expected_income_cents=income,
        expected_income_is_override=expected_income_override(session) is not None,
        fixed=tuple(fixed),
        non_monthly=tuple(non_monthly),
        flexible=tuple(flexible),
        unassigned=tuple(unassigned),
        fixed_total_cents=fixed_total,
        non_monthly_total_cents=non_monthly_total,
        goals_contribution_cents=goals,
        flex_number_cents=flex_number,
        flex_number_is_override=pool is not None,
        flexible_actual_cents=flexible_actual,
        remaining_cents=flex_number - flexible_actual,
        progress_bp=basis,
        progress_state=state,
        fixed_bucket=_bucket(fixed_total, 0, sum(row.actual_cents for row in fixed)),
        non_monthly_bucket=_bucket(
            non_monthly_total,
            sum(row.carry_cents for row in non_monthly),
            sum(row.actual_cents for row in non_monthly),
        ),
        flexible_bucket=_bucket(flex_number, 0, flexible_actual),
        unassigned_spent_cents=sum(row.actual_cents for row in unassigned),
        pace_bp=today.day * BASIS_POINTS // days if month == today.replace(day=1) else None,
    )


def category_view(session: Session, month: date, *, joint_share: bool) -> CategoryView:
    """Category mode for one month. A category with a carry but no line this month is listed too,
    with a budget of 0, so its carried amount is not hidden."""
    _check_month(month)
    categories = _categories(session)
    lines = _category_lines(session, month)
    actual = actual_by_category(session, month, joint_share=joint_share)
    carry = _carries(session, month, joint_share=joint_share)

    listed = set(lines) | {category_id for category_id, carried in carry.items() if carried.cents}
    rows = _sorted(
        [
            _row(
                categories[category_id],
                lines.get(category_id),
                actual.get(category_id, 0),
                carry.get(category_id),
            )
            for category_id in listed
        ]
    )
    budgeted = {row.category_id for row in rows}
    unbudgeted = sorted(
        (
            SpendRow(category_id, categories[category_id].name, spent)
            for category_id, spent in actual.items()
            if spent != 0 and category_id not in budgeted and category_id in categories
        ),
        key=lambda row: (-row.actual_cents, row.name.casefold(), row.category_id),
    )
    budget_total = sum(row.budget_cents for row in rows)
    carry_total = sum(row.carry_cents for row in rows)
    actual_total = sum(row.actual_cents for row in rows)
    basis, state = progress(budget_total, carry_total, actual_total)
    return CategoryView(
        month=month,
        joint_share=joint_share,
        rows=tuple(rows),
        unbudgeted=tuple(unbudgeted),
        budget_total_cents=budget_total,
        carry_total_cents=carry_total,
        actual_total_cents=actual_total,
        available_total_cents=budget_total + carry_total - actual_total,
        unbudgeted_total_cents=sum(row.actual_cents for row in unbudgeted),
        progress_bp=basis,
        progress_state=state,
    )


def progress(
    budget_cents: int, carry_cents: int, actual_cents: int
) -> tuple[int | None, ProgressState | None]:
    """Progress in floored basis points, and its state. The state is exact at the thresholds: 49.99%
    is good, 50% is warning, 99.99% is warning, and 100% is over. With nothing to spend, progress
    is None, and any spending makes it over."""
    available_before_spending = budget_cents + carry_cents
    if available_before_spending <= 0:
        return None, ProgressState.over if actual_cents > 0 else None
    basis = actual_cents * BASIS_POINTS // available_before_spending
    if basis < WARNING_BP:
        return basis, ProgressState.good
    if basis < OVER_BP:
        return basis, ProgressState.warning
    return basis, ProgressState.over


# Writes. Each one is a batch, and a repeated idempotency key returns the first batch.


def set_mode(session: Session, mode: BudgetMode | str, *, idempotency_key: str) -> Batch:
    """Switch between flex and category budgeting. No budget line is deleted or changed."""
    chosen = BudgetMode(mode)
    return _write(
        session,
        "budget_mode",
        idempotency_key,
        {"mode": chosen.value},
        lambda: [_preference_change(session, BUDGET_MODE_KEY, chosen.value)],
    )


def set_expected_income(session: Session, cents: int | None, *, idempotency_key: str) -> Batch:
    """Set the expected income you type in, or clear it (None) to use the three-month average."""
    if cents is not None:
        _check_cents(cents, "expected income")
    return _write(
        session,
        "budget_expected_income",
        idempotency_key,
        {"expected_income_cents": cents},
        lambda: [_preference_change(session, EXPECTED_INCOME_KEY, cents)],
    )


def set_line(
    session: Session,
    month: date,
    category_id: int | None,
    kind: BudgetKind | str,
    amount_cents: int,
    rollover: bool | None = None,
    *,
    idempotency_key: str,
) -> Batch:
    """Create or replace one budget line for a month, in one batch.

    rollover=None means: keep the line's current setting, or for a new line, turn rollover on for a
    non-monthly category in flex mode and off otherwise. The flex pool never rolls over.
    """
    _check_month(month)
    chosen = BudgetKind(kind)
    _check_cents(amount_cents, "a budget amount")
    if chosen is BudgetKind.category:
        if category_id is None:
            raise ValueError("a category budget needs a category")
        category = session.get(Category, category_id)
        if category is None:
            raise ValueError(f"there is no category {category_id}")
        if category.type is CategoryType.income:
            raise ValueError(f"{category.name} is an income category and has no budget")
        if category.type in NOT_IN_REPORTS:
            raise ValueError(f"{category.name} is left out of reports and has no budget")
    else:
        if category_id is not None:
            raise ValueError("the flex pool is not tied to a category")
        if rollover:
            raise ValueError("the flex pool has no rollover")
    summary = {
        "month": month.isoformat(),
        "kind": chosen.value,
        "category_id": category_id,
        "amount_cents": amount_cents,
    }
    return _write(
        session,
        "budget_set_line",
        idempotency_key,
        summary,
        lambda: [_line_change(session, month, chosen, category_id, amount_cents, rollover)],
    )


def copy_month(
    session: Session, from_month: date, to_month: date, *, idempotency_key: str
) -> Batch:
    """Copy every line of one month to another month that has none. Raises ValueError if the
    target already has lines, or if the source has none."""
    _check_month(from_month)
    _check_month(to_month)
    if from_month == to_month:
        raise ValueError("Choose a different month to copy to.")

    def build() -> list[Change]:
        if session.scalar(select(BudgetLine.id).where(BudgetLine.month == to_month).limit(1)):
            raise ValueError(f"{to_month:%B %Y} already has budget lines, so nothing is copied")
        sources = session.scalars(
            select(BudgetLine).where(BudgetLine.month == from_month).order_by(BudgetLine.id)
        ).all()
        if not sources:
            raise ValueError(f"{from_month:%B %Y} has no budget lines to copy")
        return [
            Change(
                "budget_line",
                None,
                None,
                {
                    "month": to_month,
                    "kind": line.kind,
                    "category_id": line.category_id,
                    "amount_cents": line.amount_cents,
                    "rollover": line.rollover,
                },
            )
            for line in sources
        ]

    return _write(
        session,
        "budget_copy_month",
        idempotency_key,
        {"from_month": from_month.isoformat(), "to_month": to_month.isoformat()},
        build,
    )


def fixed_fill_count(session: Session, month: date) -> int:
    """How many fixed categories quick fill would give a line to in `month`."""
    _check_month(month)
    return len(_fixed_fill_sources(session, month))


def fill_fixed_from_previous_month(session: Session, month: date, *, idempotency_key: str) -> Batch:
    """Give every visible fixed category that has no line in `month` the amount and rollover
    setting of its line in the month before. A line the month already has is never touched.
    Raises ValueError if there is nothing to fill."""
    _check_month(month)
    previous = shift_month_start(month, -1)
    sources = _fixed_fill_sources(session, month)

    def build() -> list[Change]:
        if not sources:
            raise ValueError(f"{previous:%B %Y} has no fixed budget that {month:%B %Y} lacks")
        return [
            Change(
                "budget_line",
                None,
                None,
                {
                    "month": month,
                    "kind": line.kind,
                    "category_id": line.category_id,
                    "amount_cents": line.amount_cents,
                    "rollover": line.rollover,
                },
            )
            for line in sources
        ]

    return _write(
        session,
        "budget_fill_fixed",
        idempotency_key,
        {
            "from_month": previous.isoformat(),
            "to_month": month.isoformat(),
            "category_ids": [line.category_id for line in sources],
        },
        build,
    )


def clear_flex_pool(session: Session, month: date, *, idempotency_key: str) -> Batch:
    """Remove a month's flex pool line, so its flex number is worked out again. Undo restores the
    line. Raises ValueError if the month has no flex pool line."""
    _check_month(month)

    def build() -> list[Change]:
        line_id = _line_id(session, month, BudgetKind.flex_pool, None)
        if line_id is None:
            raise ValueError(f"{month:%B %Y} has no flex number set by hand")
        before = snapshot(session, "budget_line", line_id)
        return [Change("budget_line", str(line_id), before, None)]

    return _write(
        session,
        "budget_clear_flex_pool",
        idempotency_key,
        {"month": month.isoformat()},
        build,
    )


# Helpers


def _write(
    session: Session,
    kind: str,
    idempotency_key: str,
    summary: dict[str, object],
    build: Callable[[], list[Change]],
) -> Batch:
    """Apply one batch. A key that already applied returns its batch and builds nothing."""
    existing = applied_batch_for_key(session, idempotency_key)
    if existing is not None:
        return existing
    preview = make_preview(session, kind, build(), summary)
    return apply(session, preview, idempotency_key=idempotency_key)


def _line_change(
    session: Session,
    month: date,
    kind: BudgetKind,
    category_id: int | None,
    amount_cents: int,
    rollover: bool | None,
) -> Change:
    existing_id = _line_id(session, month, kind, category_id)
    if existing_id is None:
        if rollover is None:
            rollover = _default_rollover(session, kind, category_id)
        after = {
            "month": month,
            "kind": kind,
            "category_id": category_id,
            "amount_cents": amount_cents,
            "rollover": bool(rollover),
        }
        return Change("budget_line", None, None, after)
    before = snapshot(session, "budget_line", existing_id)
    if before is None:
        raise ValueError("the budget line vanished while it was being changed")
    keep = before["rollover"] if rollover is None else rollover
    after = {**before, "amount_cents": amount_cents, "rollover": bool(keep)}
    return Change("budget_line", str(existing_id), before, after)


def _preference_change(session: Session, key: str, value: object) -> Change:
    return Change(
        "preference", key, snapshot(session, "preference", key), {"key": key, "value": value}
    )


def _default_rollover(session: Session, kind: BudgetKind, category_id: int | None) -> bool:
    if kind is not BudgetKind.category or get_mode(session) is not BudgetMode.flex:
        return False
    category = session.get(Category, category_id)
    return category is not None and category.type is CategoryType.non_monthly


def _line_id(
    session: Session, month: date, kind: BudgetKind, category_id: int | None
) -> int | None:
    query = select(BudgetLine.id).where(BudgetLine.month == month, BudgetLine.kind == kind)
    if category_id is None:
        query = query.where(BudgetLine.category_id.is_(None))
    else:
        query = query.where(BudgetLine.category_id == category_id)
    return session.scalar(query)


def _carries(session: Session, month: date, *, joint_share: bool) -> dict[int, Carry]:
    """Carry into `month` for every category with an earlier rollover line. Each such
    line contributes its budget and that month's actual. Every earlier month's actuals come from
    one report query, so a long history costs one pass over the ledger, not one per month."""
    query = select(BudgetLine.month, BudgetLine.category_id, BudgetLine.amount_cents).where(
        BudgetLine.kind == BudgetKind.category,
        BudgetLine.rollover.is_(True),
        BudgetLine.month < month,
    )
    by_month: dict[date, list] = defaultdict(list)
    for row in session.execute(query):
        by_month[row.month].append(row)
    if not by_month:
        return {}
    actuals = spending_by_month(
        session, min(by_month), month - timedelta(days=1), joint_share=joint_share
    )
    carries: dict[int, Carry] = {}
    for earlier in sorted(by_month):
        actual = actuals.get(earlier, {})
        for row in by_month[earlier]:
            so_far = carries.get(row.category_id) or Carry(0, 0, since=earlier)
            carries[row.category_id] = Carry(
                budgeted_cents=so_far.budgeted_cents + row.amount_cents,
                spent_cents=so_far.spent_cents + actual.get(row.category_id, 0),
                since=so_far.since,
            )
    return carries


def _fixed_fill_sources(session: Session, month: date) -> list[BudgetLine]:
    """The previous month's lines for fixed, visible categories that have no line in `month`."""
    has_line = select(BudgetLine.category_id).where(
        BudgetLine.month == month, BudgetLine.kind == BudgetKind.category
    )
    return list(
        session.scalars(
            select(BudgetLine)
            .join(Category, Category.id == BudgetLine.category_id)
            .where(
                BudgetLine.month == shift_month_start(month, -1),
                BudgetLine.kind == BudgetKind.category,
                Category.type == CategoryType.fixed,
                Category.hidden.is_(False),
                BudgetLine.category_id.not_in(has_line),
            )
            .order_by(BudgetLine.id)
        )
    )


def _category_lines(session: Session, month: date) -> dict[int, BudgetLine]:
    rows = session.scalars(
        select(BudgetLine).where(BudgetLine.month == month, BudgetLine.kind == BudgetKind.category)
    ).all()
    return {row.category_id: row for row in rows if row.category_id is not None}


def _flex_pool(session: Session, month: date) -> int | None:
    return session.scalar(
        select(BudgetLine.amount_cents).where(
            BudgetLine.month == month, BudgetLine.kind == BudgetKind.flex_pool
        )
    )


def _categories(session: Session) -> dict[int, Category]:
    return {category.id: category for category in session.scalars(select(Category))}


def _bucket(budget: int, carry: int, spent: int) -> BucketTotals:
    basis, state = progress(budget, carry, spent)
    return BucketTotals(
        budget_cents=budget,
        carry_cents=carry,
        spent_cents=spent,
        remaining_cents=budget + carry - spent,
        progress_bp=basis,
        progress_state=state,
    )


def _row(category: Category, line: BudgetLine | None, spent: int, carry: Carry | None) -> BudgetRow:
    budget = 0 if line is None else line.amount_cents
    carried = 0 if carry is None else carry.cents
    basis, state = progress(budget, carried, spent)
    return BudgetRow(
        category_id=category.id,
        name=category.name,
        type=category.type,
        icon=category.icon or DEFAULT_CATEGORY_ICON,
        budget_cents=budget,
        actual_cents=spent,
        carry=carry,
        rollover=line is not None and line.rollover,
        available_cents=budget + carried - spent,
        progress_bp=basis,
        progress_state=state,
    )


def _sorted(rows: list[BudgetRow]) -> list[BudgetRow]:
    return sorted(rows, key=lambda row: (row.name.casefold(), row.category_id))


def _month_period(month: date) -> Period:
    return Period(month, month_end(month))


def _check_month(month: date) -> None:
    if month.day != 1:
        raise ValueError(f"a budget month is its first day, not {month.isoformat()}")


def _check_cents(amount: int, what: str) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError(f"{what} must be an int number of cents")
    if amount < 0:
        raise ValueError(f"{what} must not be negative")
