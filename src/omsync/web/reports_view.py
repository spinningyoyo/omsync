"""View data for the reports page and its print layout: the period selection, the summary cards, the
trend chart and its table, the spending breakdown, and the links that drill down into the register.

The numbers come from omsync.reports.queries. This module formats them and builds links, as
account_page.py does for the account page. Rates arrive in basis points, so every percentage is
formatted here with integer arithmetic.
"""

from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.core.months import parse_day
from omsync.ledger.categories import TYPE_LABELS
from omsync.ledger.filters import Condition, FilterSpec
from omsync.ledger.preferences import joint_share_on
from omsync.reports.queries import (
    PRESETS,
    TREND_MONTHS,
    CategoryLine,
    Change,
    Figures,
    MonthPoint,
    Period,
    Summary,
    by_category,
    has_transactions,
    monthly_trend,
    ratio_bp,
    summary,
    uncategorized_total,
)
from omsync.web.accounts_panel import sparkline_points
from omsync.web.amounts import money_html
from omsync.web.budget_view import BudgetCard, budget_card
from omsync.web.register_state import encode_filter

CUSTOM = "custom"
DEFAULT_PRESET = "this_month"
PRESET_LABELS: dict[str, str] = {
    "this_month": "This month",
    "last_month": "Last month",
    "this_quarter": "This quarter",
    "ytd": "Year to date",
    "last_12_months": "Last 12 months",
    CUSTOM: "Custom",
}
CUSTOM_ERROR = (
    "Choose a start date and an end date, with the start on or before the end. "
    "This month is shown until you do."
)
UNKNOWN_PRESET = "That period is not one of the choices, so this month is shown."
FIGURE_TONE_GOOD = "text-success"
FIGURE_TONE_BAD = "text-destructive"


@dataclass(frozen=True)
class Selection:
    """The period the page shows. key is a preset name or "custom". start and end are ISO dates,
    or the raw text the user typed when a custom range is invalid. error says why."""

    key: str
    period: Period
    start: str
    end: str
    error: str | None = None


@dataclass(frozen=True)
class PresetLink:
    """One choice in the period menu: a plain link to the page for that period."""

    key: str
    label: str
    href: str
    active: bool


TREND_COLUMNS: list[dict[str, object]] = [
    {"header": "Month", "numeric": False},
    {"header": "Income", "numeric": True},
    {"header": "Expenses", "numeric": True},
    {"header": "Net", "numeric": True},
    {"header": "Savings rate", "numeric": True},
]


@dataclass(frozen=True)
class CardView:
    label: str
    value: str
    value_tone: str  # a text colour class, or "" for the default
    change: str | None
    change_tone: str
    arrow: str | None  # an icon name, or None when nothing moved


@dataclass(frozen=True)
class TrendRow:
    month: str
    income: str
    expenses: str
    net: str
    net_tone: str
    rate: str


@dataclass(frozen=True)
class CategoryRow:
    name: str
    type: str
    amount: str
    share: str
    bar: int  # the share as a whole percent, 0 to 100, for the bar's width
    change: str
    change_tone: str
    spark: str | None  # SVG polyline points for the last six months, or None
    href: str  # the register, filtered to this category and the period


@dataclass(frozen=True)
class ReportView:
    selection: Selection
    presets: list[PresetLink]
    state_query: str  # the selection as a query string, for links and hidden fields
    joint_note: str  # for the print layout, which has no controls
    period_label: str
    prior_label: str
    has_transactions: bool
    expenses: str
    cards: list[CardView] = field(default_factory=list)
    trend_points: list[dict[str, object]] = field(default_factory=list)
    trend_rows: list[TrendRow] = field(default_factory=list)
    trend_columns: list[dict[str, object]] = field(default_factory=lambda: list(TREND_COLUMNS))
    trend_cells: list[list[str]] = field(default_factory=list)
    categories: list[CategoryRow] = field(default_factory=list)
    uncategorized: str = ""
    print_href: str = ""
    budget: BudgetCard | None = None


def select_period(key: str, start: str, end: str, today: date) -> Selection:
    """The period a request asks for. A custom range needs two valid dates, start first. A bad
    request falls back to this month, with the reason in `error`, so the page still shows something
    true."""
    if key == CUSTOM:
        fallback = Period.preset(DEFAULT_PRESET, today)
        try:
            period = Period(parse_day(start), parse_day(end))
        except ValueError:
            return Selection(CUSTOM, fallback, start, end, CUSTOM_ERROR)
        return Selection(CUSTOM, period, period.start.isoformat(), period.end.isoformat())
    if key not in PRESETS:
        period = Period.preset(DEFAULT_PRESET, today)
        return Selection(
            DEFAULT_PRESET, period, period.start.isoformat(), period.end.isoformat(), UNKNOWN_PRESET
        )
    period = Period.preset(key, today)
    return Selection(key, period, period.start.isoformat(), period.end.isoformat())


def state_query(selection: Selection) -> str:
    """The selection as a query string: the preset, or the preset "custom" with its two dates."""
    if selection.key == CUSTOM:
        return urlencode({"period": CUSTOM, "start": selection.start, "end": selection.end})
    return urlencode({"period": selection.key})


def build_report(db: Session, selection: Selection, *, today: date) -> ReportView:
    """Everything the reports page and its print layout show for one selection."""
    joint_share = joint_share_on(db)
    period = selection.period
    result = summary(db, period, joint_share=joint_share)
    categories = by_category(db, period, joint_share=joint_share)
    trend = monthly_trend(db, TREND_MONTHS, end=period.end, joint_share=joint_share)
    trend_rows = _trend_rows(trend)
    uncategorized = uncategorized_total(db, period, joint_share=joint_share)
    query = state_query(selection)
    return ReportView(
        selection=selection,
        presets=_preset_links(selection),
        state_query=query,
        joint_note=(
            "Showing your half of joint accounts"
            if joint_share
            else "Showing joint accounts in full"
        ),
        period_label=_range(period),
        prior_label=_range(result.prior_period),
        has_transactions=has_transactions(db),
        expenses=money_html(result.figures.expenses_cents),
        cards=_cards(result),
        trend_points=_trend_chart(trend, today),
        trend_rows=trend_rows,
        trend_cells=[
            [row.month, row.income, row.expenses, row.net, row.rate] for row in trend_rows
        ],
        categories=[_category_row(row, period) for row in categories],
        uncategorized=money_html(uncategorized),
        print_href=f"/reports/print?{query}",
        budget=budget_card(db, today=today),
    )


# A change under this many basis points shows as 0.0, with one decimal of a percent.
ROUNDS_TO_ZERO_BP = 5


def percent_text(bp: int) -> str:
    """A rate in basis points as a percentage with one decimal, e.g. 1234 is "12.3%"."""
    return f"{_tenths(bp)}%"


def signed_percent(bp: int) -> str:
    """A change with its sign, e.g. "+12.3%". A change that rounds to 0.0% has no sign, since
    "-0.0%" would read as a loss."""
    return _signed(0 if abs(bp) < ROUNDS_TO_ZERO_BP else bp, percent_text(bp))


def _tenths(bp: int) -> str:
    whole, tenth = divmod((abs(bp) + 5) // 10, 10)
    return f"{whole}.{tenth}"


def _signed(bp: int, text: str) -> str:
    if bp > 0:
        return f"+{text}"
    if bp < 0:
        return f"-{text}"
    return text


def _range(period: Period) -> str:
    return f"{period.start.strftime('%b %-d, %Y')} to {period.end.strftime('%b %-d, %Y')}"


def _preset_links(selection: Selection) -> list[PresetLink]:
    links = []
    for key in (*PRESETS, CUSTOM):
        if key == CUSTOM:
            href = "/reports?" + urlencode(
                {"period": CUSTOM, "start": selection.start, "end": selection.end}
            )
        else:
            href = f"/reports?{urlencode({'period': key})}"
        links.append(
            PresetLink(
                key=key,
                label=PRESET_LABELS[key],
                href=href,
                active=selection.key == key,
            )
        )
    return links


def _cards(result: Summary) -> list[CardView]:
    figures = result.figures
    net_tone = FIGURE_TONE_GOOD if figures.net_cents >= 0 else FIGURE_TONE_BAD
    return [
        _money_card("Income", figures.income_cents, result.income_change, good_when_up=True),
        _money_card("Expenses", figures.expenses_cents, result.expenses_change, good_when_up=False),
        _money_card(
            "Net",
            figures.net_cents,
            result.net_change,
            good_when_up=True,
            value_tone=net_tone,
        ),
        _rate_card(figures, result),
    ]


def _money_card(
    label: str, cents: int, change: Change, *, good_when_up: bool, value_tone: str = ""
) -> CardView:
    delta = change.delta_cents
    if change.pct_bp is None:
        text = "No amount last period to compare" if delta else "No change"
    elif abs(change.pct_bp) < ROUNDS_TO_ZERO_BP:
        # "-0.0%" in red would read as a loss, so a change too small to show is no change.
        text, delta = "About the same as the previous period", 0
    else:
        text = f"{signed_percent(change.pct_bp)} vs previous period"
    return CardView(
        label=label,
        value=money_html(cents),
        value_tone=value_tone,
        change=text,
        change_tone=_tone(delta, good_when_up),
        arrow=_arrow(delta),
    )


def _rate_card(figures: Figures, result: Summary) -> CardView:
    delta = result.savings_rate_delta_bp
    rate = "—" if figures.savings_rate_bp is None else percent_text(figures.savings_rate_bp)
    if delta is None:
        return CardView("Savings rate", rate, "", None, "text-secondary", None)
    if abs(delta) < ROUNDS_TO_ZERO_BP:
        delta = 0
    return CardView(
        label="Savings rate",
        value=rate,
        value_tone="",
        change=f"{_signed(delta, _tenths(delta) + ' pts')} vs previous period",
        change_tone=_tone(delta, True),
        arrow=_arrow(delta),
    )


def _tone(delta: int, good_when_up: bool) -> str:
    if delta == 0:
        return "text-secondary"
    return FIGURE_TONE_GOOD if (delta > 0) == good_when_up else FIGURE_TONE_BAD


def _arrow(delta: int) -> str | None:
    if delta > 0:
        return "trending-up"
    if delta < 0:
        return "trending-down"
    return None


def _trend_chart(points: list[MonthPoint], today: date) -> list[dict[str, object]]:
    """The bar chart's data. Money is cents, and the text is formatted here, so the chart never
    rounds a value itself. The month we are in is marked partial, since it is still filling."""
    current = today.replace(day=1)
    data: list[dict[str, object]] = []
    for point in points:
        label = point.month.strftime("%b %Y" if point.month.month == 1 else "%b")
        data.append(
            {
                "label": label,
                "income": point.income_cents,
                "expense": point.expenses_cents,
                "net": point.net_cents,
                "income_text": format_cents(point.income_cents),
                "expense_text": format_cents(point.expenses_cents),
                "net_text": format_cents(point.net_cents),
                "partial": point.month == current,
            }
        )
    return data


def _trend_rows(points: list[MonthPoint]) -> list[TrendRow]:
    rows = []
    for point in points:
        rate = ratio_bp(point.net_cents, point.income_cents)
        rows.append(
            TrendRow(
                month=point.month.strftime("%b %Y"),
                income=money_html(point.income_cents),
                expenses=money_html(point.expenses_cents),
                net=money_html(point.net_cents),
                net_tone=FIGURE_TONE_GOOD if point.net_cents >= 0 else FIGURE_TONE_BAD,
                rate="—" if rate is None else percent_text(rate),
            )
        )
    return rows


def _category_row(row: CategoryLine, period: Period) -> CategoryRow:
    share = row.share_bp
    change = "—" if row.change.pct_bp is None else signed_percent(row.change.pct_bp)
    return CategoryRow(
        name=row.name,
        type=TYPE_LABELS[row.type],
        amount=money_html(row.amount_cents),
        share="—" if share is None else percent_text(share),
        bar=0 if share is None else max(0, min(100, (share + 50) // 100)),
        change=change,
        # Spending that rose is bad news, so the tone is the reverse of a figure that rose.
        change_tone=_tone(row.change.delta_cents, good_when_up=False),
        spark=sparkline_points(row.monthly_cents),
        href=_register_link(row.category_id, period),
    )


def _register_link(category_id: int, period: Period) -> str:
    """The register, filtered to one category and the period. The filter is always valid, so the
    register opens on exactly these rows."""
    spec = FilterSpec(
        conditions=(
            Condition("category", "is", (category_id,)),
            Condition("date", "between", (period.start, period.end)),
        )
    )
    return f"/transactions?{urlencode({'f': encode_filter(spec)})}"
