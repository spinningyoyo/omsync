"""View data for the home dashboard: the greeting, the period picker, the sections in the saved
order, the data the three charts draw from, and the sections that follow today, not the period.

The period numbers come from omsync.reports.dashboard, which reads the same lines as the reports.
This module formats them, builds the chart JSON, and links each outflow to the register, as
reports_view.py does. The attention counts come from omsync.reports.attention, and each row here
links to the page that lists exactly what it counts. The budget and goals cards are the Plan hub's.
Money is formatted here and nowhere else, so the charts never round a value.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.ledger.filters import Condition, FilterSpec
from omsync.ledger.models import SyncStatus
from omsync.ledger.preferences import joint_share_on
from omsync.reports.attention import UNCATEGORIZED, Attention, attention
from omsync.reports.dashboard import (
    DEFICIT_KEY,
    OTHER_NAME,
    UPCOMING_DAYS,
    Cashflow,
    FlowNode,
    Layout,
    Outflow,
    Outflows,
    SpendingTrend,
    TrendDay,
    Upcoming,
    cashflow,
    outflows,
    spending_trend,
    upcoming_expected,
)
from omsync.reports.queries import PRESETS, Period, has_transactions, ratio_bp
from omsync.web.budget_view import BudgetCard, budget_card
from omsync.web.goals_view import GoalsCard, goals_card
from omsync.web.reconcile_view import default_range
from omsync.web.register_state import encode_filter, query_string
from omsync.web.reports_view import CUSTOM, PRESET_LABELS, Selection, percent_text

SECTION_TITLES: dict[str, str] = {
    "attention": "Needs attention",
    "cash_flow": "Cash flow",
    "spending_trend": "Spending trend",
    "outflows": "Outflows",
    "upcoming": "Upcoming",
    "budget": "Budget",
    "goals": "Goals",
}
# Sections that follow today and ignore the period picker. The label says so beside the title.
SECTION_SCOPES: dict[str, str] = {
    "attention": "Today",
    "upcoming": f"Next {UPCOMING_DAYS} days",
    "budget": "This month",
    "goals": "Today",
}
SCOPE_HINT = "Relative to today, not the selected period"
REVIEW_HREF = "/transactions/review"
SYNC_HREF = "/settings/providers"
_SYNC_WORDS = {SyncStatus.failed: "failed", SyncStatus.needs_user_action: "needs your action"}
JOINT_NOTE = "Showing your half of joint accounts"
# Colours for the categories the Outflows donut draws, in the order of its segments, and the same
# colours in the Cash flow diagram. Other is grey, whatever its position.
PALETTE: tuple[str, ...] = (
    "var(--color-blue-500)",
    "var(--color-green-500)",
    "var(--color-yellow-500)",
    "var(--color-violet-500)",
    "var(--color-pink-500)",
    "var(--color-cyan-500)",
    "var(--color-orange-500)",
    "var(--color-indigo-500)",
)
OTHER_COLOR = "var(--color-gray-400)"
GOOD = "text-success"
BAD = "text-destructive"
NEUTRAL = "text-secondary"


@dataclass(frozen=True)
class PeriodLink:
    """One segment of the period picker. It is a plain link, so the page works without htmx."""

    label: str
    href: str
    active: bool
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SectionView:
    key: str
    title: str
    collapsed: bool
    half: bool
    scope: str = ""  # for the sections that follow today, not the period


@dataclass(frozen=True)
class FlowView:
    """The cash-flow section: three figures, and the sankey's data."""

    data: dict[str, Any]
    income: str
    expenses: str
    net: str
    net_tone: str
    has_flows: bool
    label: str  # the chart's accessible name, with the figures in words


@dataclass(frozen=True)
class TrendView:
    data: dict[str, Any]
    current_total: str
    previous_total: str
    change: str
    change_tone: str
    current_range: str
    previous_range: str
    has_spending: bool


@dataclass(frozen=True)
class OutflowRow:
    name: str
    amount: str
    share: str
    bar: int  # the share as a whole percent, 0 to 100, for the bar's width
    color: str
    segment: int  # the donut segment this row draws, or -1 when it draws none
    href: str | None  # the register, filtered to this category and the period; None for Other
    detail: str  # for Other: how many categories it adds up; otherwise ""


@dataclass(frozen=True)
class OutflowsView:
    data: dict[str, Any]
    rows: list[OutflowRow]
    total: str
    has_spending: bool


@dataclass(frozen=True)
class AttentionRow:
    """One thing that needs a look. count is None for a row that is one thing, such as an account.
    href opens the page that lists exactly what the count counts."""

    label: str
    detail: str
    href: str
    count: int | None = None
    badge: str = ""  # for a row with no count: what is wrong, in a word or two
    tone: str = "warning"


@dataclass(frozen=True)
class AttentionView:
    rows: list[AttentionRow]


@dataclass(frozen=True)
class UpcomingRow:
    name: str
    due: str
    amount: str
    inflow: bool


@dataclass(frozen=True)
class UpcomingView:
    rows: list[UpcomingRow]
    inflow: str
    outflow: str
    range: str


@dataclass(frozen=True)
class HomeView:
    greeting: str
    period_label: str
    selection: Selection
    presets: list[PeriodLink]
    joint_note: str  # empty when joint share is off
    has_transactions: bool
    sections: list[SectionView]
    columns: int
    flows: FlowView
    trend: TrendView
    outflows: OutflowsView
    attention: AttentionView
    upcoming: UpcomingView
    budget: BudgetCard
    goals: GoalsCard


def greeting(now: datetime) -> str:
    """A greeting for the time of day, by the hour on this machine."""
    if now.hour < 12:
        return "Good morning"
    if now.hour < 18:
        return "Good afternoon"
    return "Good evening"


def build_home(db: Session, selection: Selection, *, now: datetime, layout: Layout) -> HomeView:
    """Everything the home page shows for one period and one saved layout. The attention, upcoming,
    budget, and goals sections are as of the day of `now`, whatever the period is."""
    today = now.date()
    joint = joint_share_on(db)
    period = selection.period
    flows = cashflow(db, period, joint_share=joint)
    trend = spending_trend(db, period, joint_share=joint)
    spend = outflows(db, period, joint_share=joint)
    colors = _category_colors(spend)
    return HomeView(
        greeting=greeting(now),
        period_label=_range(period),
        selection=selection,
        presets=_presets(selection),
        joint_note=JOINT_NOTE if joint else "",
        has_transactions=has_transactions(db),
        columns=layout.columns,
        sections=[
            SectionView(
                key,
                SECTION_TITLES[key],
                key in layout.collapsed,
                key in layout.half,
                SECTION_SCOPES.get(key, ""),
            )
            for key in layout.order
        ],
        flows=_flow_view(flows, colors, period),
        trend=_trend_view(trend),
        outflows=_outflows_view(spend, period, colors),
        attention=_attention_view(
            attention(db, today=today, proposal_range=default_range(today)), today
        ),
        upcoming=_upcoming_view(upcoming_expected(db, today=today)),
        budget=budget_card(db, today=today),
        goals=goals_card(db, today=today),
    )


def _attention_view(found: Attention, today: date) -> AttentionView:
    rows: list[AttentionRow] = []
    if found.open_reviews:
        rows.append(
            AttentionRow(
                "Import reviews to resolve",
                "Statement rows that may repeat a transaction",
                REVIEW_HREF,
                found.open_reviews,
            )
        )
    if found.open_reconciliation:
        start, end = default_range(today)
        rows.append(
            AttentionRow(
                "Reconciliation to resolve",
                f"Proposals to review and transactions not matched, in the last "
                f"{(end - start).days + 1} days",
                "/reconcile?" + urlencode({"start": start.isoformat(), "end": end.isoformat()}),
                found.open_reconciliation,
            )
        )
    if found.uncategorized:
        rows.append(
            AttentionRow(
                "Uncategorized transactions",
                "On any date",
                "/transactions?" + query_string(None, UNCATEGORIZED, None),
                found.uncategorized,
            )
        )
    if found.recurring_attention:
        rows.append(
            AttentionRow(
                "Recurring needing attention",
                f"Missed or open in {today:%B}",
                "/recurring",
                found.recurring_attention,
            )
        )
    rows.extend(
        AttentionRow(
            f"{account.name} balance check fails",
            _issues_text(account.issue_count),
            f"/accounts/{account.account_id}",
            badge="Check fails",
        )
        for account in found.accounts
    )
    rows.extend(
        AttentionRow(
            f"{provider.institution_name} sync {_SYNC_WORDS[provider.status]}",
            provider.key,
            SYNC_HREF,
            badge="Needs you" if provider.status is SyncStatus.needs_user_action else "Failed",
            tone="warning" if provider.status is SyncStatus.needs_user_action else "red",
        )
        for provider in found.providers
    )
    return AttentionView(rows)


def _issues_text(count: int) -> str:
    return f"{count} to review"


def _upcoming_view(window: Upcoming) -> UpcomingView:
    return UpcomingView(
        rows=[
            UpcomingRow(
                name=item.name,
                due=_short_day(item.due_on),
                amount=format_cents(abs(item.expected_cents)),
                inflow=item.expected_cents > 0,
            )
            for item in window.items
        ],
        inflow=format_cents(window.inflow_cents),
        outflow=format_cents(window.outflow_cents),
        range=f"{_short_day(window.start)} to {_short_day(window.end)}",
    )


def _presets(selection: Selection) -> list[PeriodLink]:
    links = []
    for key in (*PRESETS, CUSTOM):
        if key == CUSTOM:
            href = "/?" + urlencode(
                {"period": CUSTOM, "start": selection.start, "end": selection.end}
            )
        else:
            href = "/?" + urlencode({"period": key})
        links.append(PeriodLink(PRESET_LABELS[key], href, selection.key == key))
    return links


def _flow_view(flows: Cashflow, colors: dict[int, str], period: Period) -> FlowView:
    income = format_cents(flows.income_cents)
    expenses = format_cents(flows.expenses_cents)
    net = format_cents(flows.net_cents)
    nodes, links = _flow_graph(flows, colors, period)
    return FlowView(
        data={"nodes": nodes, "links": links},
        income=income,
        expenses=expenses,
        net=net,
        net_tone=GOOD if flows.net_cents >= 0 else BAD,
        has_flows=bool(flows.links),
        label=f"Cash flow for {_range(flows.period)}: income {income}, expenses {expenses}, "
        f"net {net}.",
    )


def _flow_graph(
    flows: Cashflow, colors: dict[int, str], period: Period
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """The diagram's boxes and ribbons. Spending categories follow the Outflows donut: the ones it
    draws keep their colour, and the rest are folded into one grey Other box, so a long tail of
    small categories does not crowd the diagram. A category refunded past its spending is not
    folded: it flows back into the hub, as the reports count it."""
    refunded = {flows.nodes[link.source].key for link in flows.links}
    folded = [
        index
        for index, node in enumerate(flows.nodes)
        if node.key.startswith("expense:")
        and node.category_id not in colors
        and node.key not in refunded
    ]
    nodes: list[dict[str, str]] = []
    placed: dict[int, int] = {}
    other_at: int | None = None
    for index, node in enumerate(flows.nodes):
        if index not in folded:
            placed[index] = len(nodes)
            nodes.append(_flow_node(node, flows, colors, refunded, period))
        elif other_at is None:
            other_at = placed[index] = len(nodes)
            nodes.append({})
        else:
            placed[index] = other_at
    if other_at is not None:
        cents = sum(flows.nodes[index].cents for index in folded)
        nodes[other_at] = {
            "name": OTHER_NAME,
            "text": format_cents(cents),
            "color": OTHER_COLOR,
            "detail": f"{len(folded)} {'category' if len(folded) == 1 else 'categories'}",
            "share": _share_text(ratio_bp(cents, flows.expenses_cents)),
        }
    ribbons: dict[tuple[int, int], int] = {}
    for link in flows.links:
        ends = (placed[link.source], placed[link.target])
        ribbons[ends] = ribbons.get(ends, 0) + link.cents
    links = [
        {"source": source, "target": target, "value": cents, "text": format_cents(cents)}
        for (source, target), cents in ribbons.items()
    ]
    return nodes, links


def _flow_node(
    node: FlowNode, flows: Cashflow, colors: dict[int, str], refunded: set[str], period: Period
) -> dict[str, str]:
    """One box of the diagram. Income, the hub, and the surplus are green, the deficit is red, and a
    spending category takes the colour the Outflows donut gives it. A category box opens the
    register for that category and period."""
    spending = node.key.startswith("expense:")
    if node.key == DEFICIT_KEY:
        color = "var(--color-destructive)"
    elif spending:
        color = colors[node.category_id] if node.category_id in colors else OTHER_COLOR
    else:
        color = "var(--color-success)"
    view = {"name": node.name, "text": format_cents(node.cents), "color": color}
    if node.category_id is not None:
        view["href"] = _register_href(node.category_id, period)
    if spending and node.key not in refunded and flows.expenses_cents > 0:
        view["share"] = _share_text(ratio_bp(node.cents, flows.expenses_cents))
    return view


def _trend_view(trend: SpendingTrend) -> TrendView:
    delta = trend.current_total - trend.previous_total
    if delta > 0:
        change, tone = f"+{format_cents(delta)} vs previous period", BAD
    elif delta < 0:
        change, tone = f"{format_cents(delta)} vs previous period", GOOD
    else:
        change, tone = "No change vs previous period", NEUTRAL
    return TrendView(
        data={
            "current": _trend_points(trend.current),
            "previous": _trend_points(trend.previous),
        },
        current_total=format_cents(trend.current_total),
        previous_total=format_cents(trend.previous_total),
        change=change,
        change_tone=tone,
        current_range=_range(trend.period),
        previous_range=_range(trend.prior_period),
        has_spending=trend.current_total != 0 or trend.previous_total != 0,
    )


def _trend_points(days: tuple[TrendDay, ...]) -> list[dict[str, Any]]:
    return [
        {
            "day": index + 1,
            "label": point.day.strftime("%b %-d"),
            "cents": point.cents,
            "text": format_cents(point.cents),
        }
        for index, point in enumerate(days)
    ]


def _category_colors(spend: Outflows) -> dict[int, str]:
    """The colour of each category the donut draws, by category ID: the palette in the order of its
    segments. Other, and any refund-only category, draws no segment and has none."""
    drawn = [row.category_id for row in spend.rows if row.category_id is not None and row.cents > 0]
    return {category_id: PALETTE[index % len(PALETTE)] for index, category_id in enumerate(drawn)}


def _outflows_view(spend: Outflows, period: Period, colors: dict[int, str]) -> OutflowsView:
    rows = []
    segments = []
    for row in spend.rows:
        # Other, and any refund-only category, draws no segment, so it gets the neutral swatch.
        drawn = row.category_id in colors
        color = colors[row.category_id] if row.category_id in colors else OTHER_COLOR
        segment = len(segments) if drawn else -1
        rows.append(_outflow_row(row, period, color, segment))
        if drawn:
            segments.append(
                {
                    "name": row.name,
                    "cents": row.cents,
                    "text": format_cents(row.cents),
                    "share": _share_text(row.share_bp),
                    "color": color,
                }
            )
    return OutflowsView(
        data={"segments": segments, "total": format_cents(spend.total_cents)},
        rows=rows,
        total=format_cents(spend.total_cents),
        has_spending=bool(segments),
    )


def _outflow_row(row: Outflow, period: Period, color: str, segment: int) -> OutflowRow:
    is_other = row.category_id is None
    detail = ""
    if is_other and row.other_count:
        detail = f"{row.other_count} {'category' if row.other_count == 1 else 'categories'}"
    return OutflowRow(
        name=OTHER_NAME if is_other else row.name,
        amount=format_cents(row.cents),
        share=_share_text(row.share_bp),
        bar=0 if row.share_bp is None else max(0, min(100, (row.share_bp + 50) // 100)),
        color=color,
        segment=segment,
        href=None if row.category_id is None else _register_href(row.category_id, period),
        detail=detail,
    )


def _share_text(share_bp: int | None) -> str:
    """A share in basis points as a percentage. A refund-only share is negative."""
    if share_bp is None:
        return "—"
    if share_bp < 0:
        return f"-{percent_text(-share_bp)}"
    return percent_text(share_bp)


def _register_href(category_id: int, period: Period) -> str:
    spec = FilterSpec(
        conditions=(
            Condition("category", "is", (category_id,)),
            Condition("date", "between", (period.start, period.end)),
        )
    )
    return f"/transactions?{urlencode({'f': encode_filter(spec)})}"


def _range(period: Period) -> str:
    return f"{_day(period.start)} to {_day(period.end)}"


def _short_day(value: date) -> str:
    return f"{value:%a %b} {value.day}"


def _day(value: date) -> str:
    return value.strftime("%b %-d, %Y")


__all__ = [
    "SECTION_TITLES",
    "HomeView",
    "build_home",
    "greeting",
]
