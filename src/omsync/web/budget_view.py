"""View data for the budget page and the budget card: the month's tables, summary, and links to
the neighbouring months. The numbers come from omsync.plan.budgets. This module formats them and
works out what the templates need: bar widths, progress states, row order, and the forms'
idempotency keys. It makes no writes.

Progress comes from the domain's basis points, so a bar and its figure agree with budget progress
everywhere else. Money is formatted with format_cents, and no value here is a float.
"""

from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.core.months import shift_month_start
from omsync.ledger.categories import DEFAULT_CATEGORY_ICON, TYPE_LABELS, UNBUDGETED_TYPES
from omsync.ledger.models import BudgetKind, BudgetLine, Category, CategoryType
from omsync.ledger.preferences import joint_share_on
from omsync.plan import budgets
from omsync.plan.budgets import (
    BASIS_POINTS,
    BucketTotals,
    BudgetMode,
    BudgetRow,
    CategoryView,
    FlexView,
    ProgressState,
)
from omsync.plan.goals import goals_summary
from omsync.web.formats import money_input, month_key, month_title

BAR_CLASS = {"good": "bg-success", "warning": "bg-warning", "over": "bg-destructive"}

# The heading of each budgeted type's table. Flex mode and category mode both section by type.
TYPE_TITLES = {
    CategoryType.fixed: "Fixed expenses",
    CategoryType.non_monthly: "Non-monthly expenses",
    CategoryType.flexible: "Flexible spending",
    CategoryType.unassigned: "Unassigned",
}

# Table columns per flex section, and for category mode.
FIXED_COLUMNS = ("budget", "actual", "left")
NON_MONTHLY_COLUMNS = ("budget", "carry", "actual", "left", "rollover")
FLEXIBLE_COLUMNS = ("actual",)
UNASSIGNED_COLUMNS = ("actual",)
CATEGORY_COLUMNS = ("progress", "budget", "carry", "actual", "left", "rollover")


# Values the templates read


@dataclass(frozen=True)
class RowView:
    """One budget line as a table row. `budget_input` is the amount as typed when a save was
    refused, so the typed value stays in the field. `error` is that refusal."""

    category_id: int
    name: str
    icon: str
    columns: tuple[str, ...]
    editable: bool
    needs_type: bool
    key: str
    budget: str
    budget_input: str
    carry: str
    carry_budgeted: str
    carry_spent: str
    carry_since: str  # the month the carry starts from, or "" when no earlier month rolled over
    actual: str
    left: str
    left_negative: bool
    rollover: bool
    width: int
    state: str  # good, warning, over, or "" when there is nothing to spend
    bar: str
    progress: str
    over_by: str = ""
    error: str = ""


@dataclass(frozen=True)
class TotalView:
    """A section's total row. Its values are the figures the summary shows for the bucket."""

    budget: str
    carry: str
    actual: str
    left: str
    left_negative: bool


@dataclass(frozen=True)
class SectionView:
    """A flex table. `fill_count` is how many lines quick fill would add, for the fixed table."""

    key: str
    title: str
    note: str
    columns: tuple[str, ...]
    rows: tuple[RowView, ...]
    total: TotalView | None = None
    fill_count: int = 0


@dataclass(frozen=True)
class TypeSection:
    """Category mode: the budgeted categories of one type. `fill_count` is as for SectionView."""

    name: str
    rows: tuple[RowView, ...]
    fill_count: int = 0


@dataclass(frozen=True)
class BucketView:
    """A flex bucket in the summary card and the hub card. `rolled` marks a non-monthly bucket
    that carries an amount in. `pace` is the month's progress as a whole percent, or None."""

    key: str
    name: str
    spent: str
    left: str
    left_negative: bool
    rolled: bool
    width: int
    bar: str
    state: str
    progress: str
    pace: int | None = None


@dataclass(frozen=True)
class UnbudgetedRow:
    category_id: int
    name: str
    icon: str
    type: str  # the type's label, such as Flexible
    actual: str
    key: str


@dataclass(frozen=True)
class GoalLine:
    goal_id: int
    name: str
    amount: str


@dataclass(frozen=True)
class FlexSummary:
    income: str
    income_input: str
    income_is_override: bool
    fixed: str
    non_monthly: str
    goals: str
    flex_number: str
    flex_note: str
    flex_negative: bool
    flex_is_set: bool
    flex_input: str
    flex_placeholder: str
    buckets: tuple[BucketView, ...]
    unassigned_spent: str


@dataclass(frozen=True)
class CategorySummary:
    budget: str
    carry: str
    actual: str
    left: str
    left_negative: bool
    width: int
    state: str
    bar: str
    progress: str
    over: tuple[RowView, ...]
    over_count: int
    unbudgeted: str


@dataclass(frozen=True)
class BudgetPage:
    key: str
    title: str
    prev_key: str
    prev_title: str
    next_key: str
    next_title: str
    current_key: str
    is_current: bool
    mode: str
    is_flex: bool
    joint_note: str
    copy_from: str
    mode_key: str
    income_key: str
    average_key: str
    pool_key: str
    pool_reset_key: str
    copy_key: str
    fill_key: str
    flex: FlexSummary | None
    sections: tuple[SectionView, ...]
    contributions: tuple[GoalLine, ...]
    contributions_total: str
    category: CategorySummary | None
    type_sections: tuple[TypeSection, ...]
    unbudgeted: tuple[UnbudgetedRow, ...]

    def find_row(self, category_id: int) -> RowView | None:
        """The row for a category in whichever table shows it, or None."""
        for section in self.sections:
            for row in section.rows:
                if row.category_id == category_id:
                    return row
        for type_section in self.type_sections:
            for row in type_section.rows:
                if row.category_id == category_id:
                    return row
        return None


@dataclass(frozen=True)
class BudgetCard:
    """The budget card on the Plan hub and on Reports. Flex mode fills the flex fields, and category
    mode fills the category fields. `empty` means category mode has no budget yet."""

    month_label: str
    key: str
    href: str
    is_flex: bool
    empty: bool
    headline_label: str
    headline: str
    headline_note: str
    spent_label: str
    spent: str
    left: str
    left_negative: bool
    width: int
    state: str
    bar: str
    progress: str
    over: tuple[RowView, ...]
    buckets: tuple[BucketView, ...] = ()


# Budget page


def budget_page(db: Session, month: date, *, today: date) -> BudgetPage:
    """Everything the budget page shows for one month, in the mode you chose."""
    joint = joint_share_on(db)
    categories = _categories(db)
    mode = budgets.get_mode(db)
    source = shift_month_start(month, -1)
    has_lines = _has_lines(db, month)
    copy_from = month_title(source) if not has_lines and _has_lines(db, source) else ""
    contributions = _contributions(db, today)
    fill_count = budgets.fixed_fill_count(db, month)
    common = {
        "key": month_key(month),
        "title": month_title(month),
        "prev_key": month_key(source),
        "prev_title": month_title(source),
        "next_key": month_key(shift_month_start(month, 1)),
        "next_title": month_title(shift_month_start(month, 1)),
        "current_key": month_key(today.replace(day=1)),
        "is_current": month == today.replace(day=1),
        "mode": mode.value,
        "joint_note": "Showing your half of joint accounts, as in Reports." if joint else "",
        "copy_from": copy_from,
        "mode_key": uuid4().hex,
        "income_key": uuid4().hex,
        "average_key": uuid4().hex,
        "pool_key": uuid4().hex,
        "pool_reset_key": uuid4().hex,
        "copy_key": uuid4().hex,
        "fill_key": uuid4().hex,
        "contributions": contributions,
        "contributions_total": format_cents(budgets.goals_contribution_cents(db)),
    }
    if mode is BudgetMode.flex:
        flex = budgets.flex_view(db, month, joint_share=joint, today=today)
        return BudgetPage(
            **common,
            is_flex=True,
            flex=_flex_summary(flex),
            sections=_flex_sections(flex, categories, fill_count),
            category=None,
            type_sections=(),
            unbudgeted=(),
        )
    view = budgets.category_view(db, month, joint_share=joint)
    return BudgetPage(
        **common,
        is_flex=False,
        flex=None,
        sections=(),
        category=_category_summary(view),
        type_sections=_category_type_sections(view, fill_count),
        unbudgeted=_unbudgeted(view, categories),
    )


def _flex_summary(flex: FlexView) -> FlexSummary:
    pool = flex.flex_number_is_override
    computed = (
        flex.expected_income_cents
        - flex.fixed_total_cents
        - flex.non_monthly_total_cents
        - flex.goals_contribution_cents
    )
    return FlexSummary(
        income=format_cents(flex.expected_income_cents),
        income_input=money_input(flex.expected_income_cents),
        income_is_override=flex.expected_income_is_override,
        fixed=format_cents(flex.fixed_total_cents),
        non_monthly=format_cents(flex.non_monthly_total_cents),
        goals=format_cents(flex.goals_contribution_cents),
        flex_number=format_cents(flex.flex_number_cents),
        flex_note=_flex_note(pool),
        flex_negative=flex.flex_number_cents < 0,
        flex_is_set=pool,
        flex_input=money_input(flex.flex_number_cents) if pool else "",
        flex_placeholder=money_input(computed),
        buckets=_buckets(flex),
        unassigned_spent=format_cents(flex.unassigned_spent_cents)
        if flex.unassigned_spent_cents
        else "",
    )


def _flex_note(is_override: bool) -> str:
    """What the flex number is, under it on the budget page and on the hub card."""
    if is_override:
        return "Set by hand for this month."
    return "Expected income less fixed, non-monthly, and goal contributions."


def _buckets(flex: FlexView) -> tuple[BucketView, ...]:
    """Fixed, flexible, and non-monthly, as the summary lists them. Only flexible has a pace."""
    specs = (
        ("fixed", "Fixed", flex.fixed_bucket, None),
        ("flexible", "Flexible", flex.flexible_bucket, flex.pace_bp),
        ("non_monthly", "Non-monthly", flex.non_monthly_bucket, None),
    )
    views = []
    for key, name, bucket, pace_bp in specs:
        width, bar, state, label = _progress(
            bucket.progress_bp, bucket.progress_state, bucket.spent_cents
        )
        views.append(
            BucketView(
                key=key,
                name=name,
                spent=format_cents(bucket.spent_cents),
                left=format_cents(bucket.remaining_cents),
                left_negative=bucket.remaining_cents < 0,
                rolled=bucket.carry_cents != 0,
                width=width,
                bar=bar,
                state=state,
                progress=label,
                pace=None if pace_bp is None else pace_bp // 100,
            )
        )
    return tuple(views)


def _flex_sections(
    flex: FlexView, categories: dict[int, Category], fill_count: int
) -> tuple[SectionView, ...]:
    """The four flex tables. Categories with no line and no spending are listed too, so a budget
    can be set for them. Income, excluded, and hidden categories are not listed."""
    buckets: dict[CategoryType, list[BudgetRow]] = {
        CategoryType.fixed: list(flex.fixed),
        CategoryType.non_monthly: list(flex.non_monthly),
        CategoryType.flexible: list(flex.flexible),
        CategoryType.unassigned: list(flex.unassigned),
    }
    present = {row.category_id for rows in buckets.values() for row in rows}
    for category in categories.values():
        if category.type in UNBUDGETED_TYPES or category.hidden or category.id in present:
            continue
        buckets[category.type].append(_blank_row(category))

    specs = (
        (
            CategoryType.fixed,
            "fixed",
            TYPE_TITLES[CategoryType.fixed],
            "Predictable costs, such as rent and insurance. Their budgets come off the top.",
            FIXED_COLUMNS,
            True,
        ),
        (
            CategoryType.non_monthly,
            "non_monthly",
            TYPE_TITLES[CategoryType.non_monthly],
            "Costs that come a few times a year. Each month saves for them, and they roll over.",
            NON_MONTHLY_COLUMNS,
            True,
        ),
        (
            CategoryType.flexible,
            "flexible",
            TYPE_TITLES[CategoryType.flexible],
            "Day-to-day spending, shown as actuals. The flex number is what it may total.",
            FLEXIBLE_COLUMNS,
            False,
        ),
        (
            CategoryType.unassigned,
            "unassigned",
            TYPE_TITLES[CategoryType.unassigned],
            "These categories have no type yet, so they are not in the flex number.",
            UNASSIGNED_COLUMNS,
            False,
        ),
    )
    totals = {
        CategoryType.fixed: _total(flex.fixed_bucket),
        CategoryType.non_monthly: _total(flex.non_monthly_bucket),
        CategoryType.flexible: _total(flex.flexible_bucket),
        CategoryType.unassigned: TotalView(
            budget="",
            carry="",
            actual=format_cents(flex.unassigned_spent_cents),
            left="",
            left_negative=False,
        ),
    }
    sections = []
    for category_type, key, title, note, columns, editable in specs:
        rows = sorted(buckets[category_type], key=_row_order)
        views = tuple(_row(row, columns=columns, editable=editable) for row in rows)
        sections.append(
            SectionView(
                key=key,
                title=title,
                note=note,
                columns=columns,
                rows=views,
                total=totals[category_type],
                fill_count=fill_count if category_type is CategoryType.fixed else 0,
            )
        )
    return tuple(sections)


def _total(bucket: BucketTotals) -> TotalView:
    return TotalView(
        budget=format_cents(bucket.budget_cents),
        carry=format_cents(bucket.carry_cents),
        actual=format_cents(bucket.spent_cents),
        left=format_cents(bucket.remaining_cents),
        left_negative=bucket.remaining_cents < 0,
    )


def _category_summary(view: CategoryView) -> CategorySummary:
    width, bar, state, label = _progress(
        view.progress_bp, view.progress_state, view.actual_total_cents
    )
    over = sorted(
        (row for row in view.rows if row.progress_state is ProgressState.over),
        key=lambda row: (
            -(row.actual_cents - row.budget_cents - row.carry_cents),
            row.name.casefold(),
        ),
    )
    over_rows = tuple(_row(row, columns=CATEGORY_COLUMNS, editable=True) for row in over)
    return CategorySummary(
        budget=format_cents(view.budget_total_cents),
        carry=format_cents(view.carry_total_cents),
        actual=format_cents(view.actual_total_cents),
        left=format_cents(view.available_total_cents),
        left_negative=view.available_total_cents < 0,
        width=width,
        state=state,
        bar=bar,
        progress=label,
        over=over_rows[:3],
        over_count=len(over_rows),
        unbudgeted=format_cents(view.unbudgeted_total_cents),
    )


def _category_type_sections(view: CategoryView, fill_count: int) -> tuple[TypeSection, ...]:
    """The budgeted categories, a table for each type, in the order of the flex sections. The fixed
    section is listed without rows when only quick fill can give it any."""
    by_type: dict[CategoryType, list[BudgetRow]] = {}
    for row in view.rows:
        by_type.setdefault(row.type, []).append(row)
    if fill_count:
        by_type.setdefault(CategoryType.fixed, [])
    return tuple(
        TypeSection(
            name=TYPE_TITLES.get(category_type, category_type.value.capitalize()),
            rows=tuple(
                _row(row, columns=CATEGORY_COLUMNS, editable=True)
                for row in sorted(by_type[category_type], key=_row_order)
            ),
            fill_count=fill_count if category_type is CategoryType.fixed else 0,
        )
        for category_type in CategoryType
        if category_type in by_type
    )


def _unbudgeted(view: CategoryView, categories: dict[int, Category]) -> tuple[UnbudgetedRow, ...]:
    """Spending categories with no line this month, then the rest, each with an Add budget form.
    Spending comes first, largest first."""
    budgeted = {row.category_id for row in view.rows}
    spent = {row.category_id: row.actual_cents for row in view.unbudgeted}
    candidates = [
        category
        for category in categories.values()
        if category.type not in UNBUDGETED_TYPES
        and not category.hidden
        and category.id not in budgeted
    ]
    candidates.sort(key=lambda c: (-spent.get(c.id, 0), c.name.casefold(), c.id))
    return tuple(
        UnbudgetedRow(
            category_id=category.id,
            name=category.name,
            icon=category.icon or DEFAULT_CATEGORY_ICON,
            type=TYPE_LABELS[category.type],
            actual=format_cents(spent.get(category.id, 0)),
            key=uuid4().hex,
        )
        for category in candidates
    )


def _contributions(db: Session, today: date) -> tuple[GoalLine, ...]:
    summary = goals_summary(db, today=today)
    return tuple(
        GoalLine(goal.id, goal.name, format_cents(goal.monthly_contribution_cents))
        for goal, _progress_row in summary.rows
        if goal.monthly_contribution_cents > 0
    )


def _row(
    row: BudgetRow,
    *,
    columns: tuple[str, ...],
    editable: bool,
) -> RowView:
    width, bar, state, label = _progress(row.progress_bp, row.progress_state, row.actual_cents)
    over_by = row.actual_cents - row.budget_cents - row.carry_cents
    carry = row.carry
    return RowView(
        category_id=row.category_id,
        name=row.name,
        icon=row.icon,
        columns=columns,
        editable=editable,
        needs_type=row.type is CategoryType.unassigned,
        key=uuid4().hex,
        budget=format_cents(row.budget_cents),
        budget_input=money_input(row.budget_cents),
        carry=format_cents(row.carry_cents),
        carry_budgeted=format_cents(carry.budgeted_cents) if carry else "",
        carry_spent=format_cents(carry.spent_cents) if carry else "",
        carry_since=month_title(carry.since) if carry else "",
        actual=format_cents(row.actual_cents),
        left=format_cents(row.available_cents),
        left_negative=row.available_cents < 0,
        rollover=row.rollover,
        width=width,
        state=state,
        bar=bar,
        progress=label,
        over_by=format_cents(over_by) if state == "over" and over_by > 0 else "",
    )


def _progress(
    basis_points: int | None, state: ProgressState | None, actual_cents: int
) -> tuple[int, str, str, str]:
    """Bar width in whole percent, bar colour class, state name, and label for one progress value.
    Nothing to spend and nothing spent gives an empty bar with a dash. Spending with no budget is a
    full over bar."""
    if state is None:
        return 0, "bg-surface-inset", "", "—"
    name = state.value
    if basis_points is None:
        return 100, BAR_CLASS[name], name, "No budget"
    width = min(basis_points, BASIS_POINTS) // 100
    return width, BAR_CLASS[name], name, f"{basis_points // 100}%"


def _blank_row(category: Category) -> BudgetRow:
    return BudgetRow(
        category_id=category.id,
        name=category.name,
        type=category.type,
        icon=category.icon or DEFAULT_CATEGORY_ICON,
        budget_cents=0,
        actual_cents=0,
        carry=None,
        rollover=False,
        available_cents=0,
        progress_bp=None,
        progress_state=None,
    )


def _row_order(row: BudgetRow) -> tuple[str, int]:
    return row.name.casefold(), row.category_id


def _categories(db: Session) -> dict[int, Category]:
    return {category.id: category for category in db.scalars(select(Category))}


def _has_lines(db: Session, month: date) -> bool:
    return db.scalar(select(BudgetLine.id).where(BudgetLine.month == month).limit(1)) is not None


def line_amount(db: Session, month: date, category_id: int) -> int:
    """The amount of a category's line in a month, or 0 when there is no line."""
    amount = db.scalar(
        select(BudgetLine.amount_cents).where(
            BudgetLine.month == month,
            BudgetLine.kind == BudgetKind.category,
            BudgetLine.category_id == category_id,
        )
    )
    return 0 if amount is None else int(amount)


# Budget card (hub and Reports)


def budget_card(db: Session, *, today: date) -> BudgetCard:
    """The budget card for the month that contains `today`: the flex number in flex mode, the totals
    in category mode. It needs only the session and today, so any page can show it."""
    month = today.replace(day=1)
    joint = joint_share_on(db)
    mode = budgets.get_mode(db)
    common = {
        "month_label": month_title(month),
        "key": month_key(month),
        "href": f"/plan/budget/{month_key(month)}",
    }
    if mode is BudgetMode.flex:
        flex = budgets.flex_view(db, month, joint_share=joint, today=today)
        width, bar, state, label = _progress(
            flex.progress_bp, flex.progress_state, flex.flexible_actual_cents
        )
        return BudgetCard(
            **common,
            is_flex=True,
            empty=False,
            headline_label="Flex number",
            headline=format_cents(flex.flex_number_cents),
            headline_note=_flex_note(flex.flex_number_is_override),
            spent_label="Flexible spent",
            spent=format_cents(flex.flexible_actual_cents),
            left=format_cents(flex.remaining_cents),
            left_negative=flex.remaining_cents < 0,
            width=width,
            state=state,
            bar=bar,
            progress=label,
            over=(),
            buckets=_buckets(flex),
        )
    view = budgets.category_view(db, month, joint_share=joint)
    width, bar, state, label = _progress(
        view.progress_bp, view.progress_state, view.actual_total_cents
    )
    over = _category_summary(view).over
    return BudgetCard(
        **common,
        is_flex=False,
        empty=not view.rows,
        headline_label="Budgeted",
        headline=format_cents(view.budget_total_cents),
        headline_note="",
        spent_label="Spent",
        spent=format_cents(view.actual_total_cents),
        left=format_cents(view.available_total_cents),
        left_negative=view.available_total_cents < 0,
        width=width,
        state=state,
        bar=bar,
        progress=label,
        over=over,
    )
