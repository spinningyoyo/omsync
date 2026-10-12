"""The home dashboard's numbers and the saved layout of its sections.

The period sections are built from the same lines as the reports, so each figure equals the report's
figure for the same period and joint share setting. This module only arranges those lines: the
cash-flow sankey, the cumulative spending trend, and the outflows breakdown. It reads lines through
queries.report_lines, and it takes its shares from by_category. The Upcoming section is relative
to today, not to the period: the confirmed recurring occurrences still to come (see
upcoming_expected). The attention counts are in attention.py, and the budget and goals cards come
from the Plan builders.

The section layout (the order of the sections, which are collapsed, which take half the width, and
how many columns the page uses) is the view preference dashboard_layout. It is not ledger data, so
it is written directly, as preferences are.

Every amount is integer cents. Nothing here formats text, and nothing here imports omsync.web.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from omsync.ledger.preferences import get_pref, set_pref
from omsync.plan.recurring import OccurrenceStatus, upcoming
from omsync.reports.queries import Period, ReportLine, by_category, ratio_bp, report_lines

# The default order. A section missing from a saved layout is inserted at its place in this order.
SECTIONS: tuple[str, ...] = (
    "attention",
    "cash_flow",
    "spending_trend",
    "outflows",
    "upcoming",
    "budget",
    "goals",
)
# Sections that share a row by default: the two period charts, then Upcoming beside Budget.
DEFAULT_HALF = frozenset({"spending_trend", "outflows", "upcoming", "budget"})
UPCOMING_DAYS = 14
DASHBOARD_LAYOUT = "dashboard_layout"
OUTFLOW_ROWS = 8
OTHER_NAME = "Other"
CASH_FLOW_NAME = "Cash flow"
SURPLUS_NAME = "Surplus"
DEFICIT_NAME = "Deficit"
CENTER_KEY = "center"
SURPLUS_KEY = "surplus"
DEFICIT_KEY = "deficit"
MAX_LAYOUT_CHARS = 2048
MAX_LAYOUT_ITEMS = 32
MAX_COLUMNS = 2
MAX_KEY_CHARS = 64


class LayoutError(ValueError):
    """A saved layout that cannot be read. Its message says what is wrong, for the reply."""


# Cash flow


@dataclass(frozen=True)
class FlowNode:
    """One box of the sankey. cents is the value that passes through it. The category ID is set
    for income and expense nodes only."""

    key: str  # "center", "surplus", "deficit", "income:<id>", or "expense:<id>"
    name: str
    cents: int
    category_id: int | None = None


@dataclass(frozen=True)
class FlowLink:
    """A flow from nodes[source] to nodes[target]. Both are indexes into Cashflow.nodes."""

    source: int
    target: int
    cents: int


@dataclass(frozen=True)
class Cashflow:
    """The sankey for one period. Income categories feed the center node, and the center feeds the
    expense categories and Surplus, or Deficit feeds it. A category whose net runs against its side
    (a refund in an expense category, a reversal in an income category) flows the other way, so the
    flows balance at the center to the cent. The first node is the center."""

    period: Period
    income_cents: int
    expenses_cents: int
    net_cents: int
    nodes: tuple[FlowNode, ...]
    links: tuple[FlowLink, ...]


@dataclass(frozen=True)
class _Total:
    category_id: int
    name: str
    is_income: bool
    cents: int  # what the category adds to income, or to expenses, with the reports' sign


def cashflow(session: Session, period: Period, *, joint_share: bool) -> Cashflow:
    totals = _totals(report_lines(session, period, joint_share=joint_share))
    income = _ordered([t for t in totals if t.is_income and t.cents != 0])
    spending = _ordered([t for t in totals if not t.is_income and t.cents != 0])
    income_cents = sum(t.cents for t in totals if t.is_income)
    expenses_cents = sum(t.cents for t in totals if not t.is_income)
    net = income_cents - expenses_cents
    center_cents = (
        sum(t.cents for t in income if t.cents > 0)
        + sum(-t.cents for t in spending if t.cents < 0)
        + max(-net, 0)
    )

    nodes = [FlowNode(CENTER_KEY, CASH_FLOW_NAME, center_cents)]
    links: list[FlowLink] = []
    for total in income:
        index = len(nodes)
        nodes.append(_category_node("income", total))
        if total.cents > 0:
            links.append(FlowLink(index, 0, total.cents))
        else:
            links.append(FlowLink(0, index, -total.cents))
    for total in spending:
        index = len(nodes)
        nodes.append(_category_node("expense", total))
        if total.cents > 0:
            links.append(FlowLink(0, index, total.cents))
        else:
            links.append(FlowLink(index, 0, -total.cents))
    if net > 0:
        nodes.append(FlowNode(SURPLUS_KEY, SURPLUS_NAME, net))
        links.append(FlowLink(0, len(nodes) - 1, net))
    elif net < 0:
        nodes.append(FlowNode(DEFICIT_KEY, DEFICIT_NAME, -net))
        links.append(FlowLink(len(nodes) - 1, 0, -net))

    return Cashflow(
        period=period,
        income_cents=income_cents,
        expenses_cents=expenses_cents,
        net_cents=net,
        nodes=tuple(nodes),
        links=tuple(links),
    )


def _category_node(side: str, total: _Total) -> FlowNode:
    return FlowNode(
        f"{side}:{total.category_id}", total.name, abs(total.cents), category_id=total.category_id
    )


def _totals(lines: Sequence[ReportLine]) -> list[_Total]:
    """Each categorized category's net, with the reports' sign: income as is, expenses negated so
    spending is positive. Uncategorized lines are left out, as they are from income and expenses."""
    sums: dict[int, int] = {}
    names: dict[int, tuple[str, bool]] = {}
    for line in lines:
        if line.category_id is None:
            continue
        sums[line.category_id] = sums.get(line.category_id, 0) + line.amount_cents
        names[line.category_id] = (line.category_name or "", line.is_income)
    totals = []
    for category_id, cents in sums.items():
        name, is_income = names[category_id]
        totals.append(_Total(category_id, name, is_income, cents if is_income else -cents))
    return totals


def _ordered(totals: Iterable[_Total]) -> list[_Total]:
    """Largest first, then by name, so the chart and the tests see the same order every time."""
    return sorted(totals, key=lambda t: (-t.cents, t.name.casefold(), t.category_id))


# Spending trend


@dataclass(frozen=True)
class TrendDay:
    day: date  # a day of the period, or of the period before it
    cents: int  # spending from the first day through this one, so it is cumulative


@dataclass(frozen=True)
class SpendingTrend:
    """Cumulative spending for the period and for the period before it of the same length. Both
    lists hold one point per day, and the points line up by position: the first day with the
    first. The last point of each equals that period's expenses in the reports."""

    period: Period
    prior_period: Period
    current: tuple[TrendDay, ...]
    previous: tuple[TrendDay, ...]

    @property
    def current_total(self) -> int:
        return self.current[-1].cents if self.current else 0

    @property
    def previous_total(self) -> int:
        return self.previous[-1].cents if self.previous else 0


def spending_trend(session: Session, period: Period, *, joint_share: bool) -> SpendingTrend:
    prior = period.previous()
    return SpendingTrend(
        period=period,
        prior_period=prior,
        current=_cumulative(session, period, joint_share=joint_share),
        previous=_cumulative(session, prior, joint_share=joint_share),
    )


def _cumulative(session: Session, period: Period, *, joint_share: bool) -> tuple[TrendDay, ...]:
    daily = [0] * period.days
    for line in report_lines(session, period, joint_share=joint_share):
        if line.category_id is None or line.is_income:
            continue
        daily[(line.posted_on - period.start).days] -= line.amount_cents
    days = []
    running = 0
    for offset, spent in enumerate(daily):
        running += spent
        days.append(TrendDay(period.start + timedelta(days=offset), running))
    return tuple(days)


# Outflows


@dataclass(frozen=True)
class Outflow:
    """One row of the outflows breakdown. category_id is None for Other."""

    category_id: int | None
    name: str
    cents: int
    share_bp: int | None  # of the period's expenses; None when they total 0
    other_count: int = 0  # for Other: how many categories it adds up


@dataclass(frozen=True)
class Outflows:
    """Expenses by category, largest first: the top eight, then Other. Other is everything else,
    net of any refund categories, so the rows add up to the period's expenses exactly."""

    period: Period
    total_cents: int
    rows: tuple[Outflow, ...]


def outflows(session: Session, period: Period, *, joint_share: bool) -> Outflows:
    categories = [
        row for row in by_category(session, period, joint_share=joint_share) if row.amount_cents
    ]
    total = sum(row.amount_cents for row in categories)
    shown = [row for row in categories if row.amount_cents > 0][:OUTFLOW_ROWS]
    shown_ids = {row.category_id for row in shown}
    rows = [
        Outflow(row.category_id, row.name, row.amount_cents, ratio_bp(row.amount_cents, total))
        for row in shown
    ]
    rest = [row for row in categories if row.category_id not in shown_ids]
    if rest:
        other = total - sum(row.cents for row in rows)
        rows.append(Outflow(None, OTHER_NAME, other, ratio_bp(other, total), other_count=len(rest)))
    return Outflows(period=period, total_cents=total, rows=tuple(rows))


# Upcoming


@dataclass(frozen=True)
class UpcomingItem:
    series_id: int
    name: str
    due_on: date
    expected_cents: int  # signed as the series is: a bill is negative, a paycheck positive


@dataclass(frozen=True)
class Upcoming:
    """The confirmed recurring occurrences still to come in the next UPCOMING_DAYS days, today
    included. Occurrences already paid are in the ledger, so they are not repeated here. The totals
    are magnitudes. These are expectations, not ledger money."""

    start: date
    end: date
    items: tuple[UpcomingItem, ...]
    inflow_cents: int
    outflow_cents: int


def upcoming_expected(session: Session, *, today: date) -> Upcoming:
    end = today + timedelta(days=UPCOMING_DAYS - 1)
    items = tuple(
        UpcomingItem(item.series_id, item.series_name, item.due_on, item.expected_cents)
        for item in upcoming(session, start=today, end=end, today=today)
        if item.status is OccurrenceStatus.upcoming
    )
    return Upcoming(
        start=today,
        end=end,
        items=items,
        inflow_cents=sum(item.expected_cents for item in items if item.expected_cents > 0),
        outflow_cents=-sum(item.expected_cents for item in items if item.expected_cents < 0),
    )


# The saved layout


@dataclass(frozen=True)
class Layout:
    """Every section in display order, the collapsed ones, and how wide each is. `columns` is how
    many columns the page uses where there is room for two. A half-width section takes one column
    and a full-width one spans both; with a single column every section is full width."""

    order: tuple[str, ...]
    collapsed: frozenset[str]
    half: frozenset[str]
    columns: int


def default_layout() -> Layout:
    return Layout(order=SECTIONS, collapsed=frozenset(), half=DEFAULT_HALF, columns=MAX_COLUMNS)


def normalize_layout(
    order: Iterable[str],
    collapsed: Iterable[str],
    half: Iterable[str] | None = None,
    columns: int = MAX_COLUMNS,
) -> Layout:
    """Known sections only, each once, in the saved order. Unknown keys are ignored. A section
    missing from the order goes after the nearest section before it in the default order that is
    present, or first when none is, so a section added later lands where it belongs by default
    rather than at the end. A layout that says nothing about widths takes the default widths."""
    ordered = list(dict.fromkeys(key for key in order if key in SECTIONS))
    for index, key in enumerate(SECTIONS):
        if key not in ordered:
            ordered.insert(_default_position(ordered, SECTIONS[:index]), key)
    return Layout(
        order=tuple(ordered),
        collapsed=frozenset(key for key in collapsed if key in SECTIONS),
        half=DEFAULT_HALF if half is None else frozenset(key for key in half if key in SECTIONS),
        columns=columns,
    )


def _default_position(ordered: list[str], preceding: Sequence[str]) -> int:
    for key in reversed(preceding):
        if key in ordered:
            return ordered.index(key) + 1
    return 0


def load_layout(session: Session) -> Layout:
    """The saved layout, or the default when none is saved or the saved value cannot be read."""
    stored = get_pref(session, DASHBOARD_LAYOUT, None)
    if not isinstance(stored, dict):
        return default_layout()
    try:
        return _read_layout(stored)
    except LayoutError:
        return default_layout()


def save_layout(session: Session, layout: Layout) -> None:
    set_pref(
        session,
        DASHBOARD_LAYOUT,
        {
            "order": list(layout.order),
            "collapsed": sorted(layout.collapsed),
            "half": sorted(layout.half),
            "columns": layout.columns,
        },
    )


def parse_layout(text: str) -> Layout:
    """Read the layout a browser sends: a JSON object with lists of section keys under "order" and
    "collapsed", and optionally a list under "half" and a column count under "columns". Raises
    LayoutError when the text is not that shape."""
    if len(text) > MAX_LAYOUT_CHARS:
        raise LayoutError("The layout is too large.")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LayoutError("The layout is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise LayoutError("The layout must be a JSON object.")
    return _read_layout(data)


def _read_layout(data: dict[str, Any]) -> Layout:
    order, collapsed, half = data.get("order"), data.get("collapsed"), data.get("half")
    columns = data.get("columns", MAX_COLUMNS)
    if not _is_str_list(order) or not _is_str_list(collapsed):
        raise LayoutError('"order" and "collapsed" must each be a list of section names.')
    if half is not None and not _is_str_list(half):
        raise LayoutError('"half" must be a list of section names.')
    if len(order) > MAX_LAYOUT_ITEMS or len(collapsed) > MAX_LAYOUT_ITEMS:
        raise LayoutError("The layout lists too many sections.")
    if half is not None and len(half) > MAX_LAYOUT_ITEMS:
        raise LayoutError("The layout lists too many sections.")
    if type(columns) is not int or not 1 <= columns <= MAX_COLUMNS:
        raise LayoutError(f'"columns" must be 1 to {MAX_COLUMNS}.')
    return normalize_layout(order, collapsed, half, columns)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and len(item) <= MAX_KEY_CHARS for item in value
    )


__all__ = [
    "DEFAULT_HALF",
    "MAX_COLUMNS",
    "SECTIONS",
    "Cashflow",
    "FlowLink",
    "FlowNode",
    "Layout",
    "LayoutError",
    "Outflow",
    "Outflows",
    "SpendingTrend",
    "TrendDay",
    "UPCOMING_DAYS",
    "Upcoming",
    "UpcomingItem",
    "cashflow",
    "default_layout",
    "load_layout",
    "normalize_layout",
    "outflows",
    "parse_layout",
    "save_layout",
    "spending_trend",
    "upcoming_expected",
]
