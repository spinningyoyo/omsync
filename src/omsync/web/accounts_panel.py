"""The accounts panel's view: open accounts grouped by type or by institution. Type groups carry a
subtotal, its latest balance, a sparkline, a joint badge, and a warning when its balance check has
issues. Institution groups carry no subtotal, because netting a card's debt against a checking
balance means nothing.

The sparkline is an inline SVG polyline computed here, so the panel needs no JavaScript.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.ledger.accounts_view import AccountSummary, account_summaries, group_totals
from omsync.ledger.models import AccountType
from omsync.ledger.preferences import get_pref, set_pref
from omsync.ledger.sources import account_digits

TYPE_LABELS: dict[AccountType, str] = {
    AccountType.checking: "Checking",
    AccountType.savings: "Savings",
    AccountType.credit_card: "Credit card",
    AccountType.brokerage: "Brokerage",
    AccountType.other: "Other",
}
GROUP_BY_TYPE = "type"
GROUP_BY_INSTITUTION = "institution"
GROUPINGS = (GROUP_BY_TYPE, GROUP_BY_INSTITUTION)
GROUPING_PREF = "accounts_panel_grouping"
SPARK_WIDTH = 56
SPARK_HEIGHT = 20
EM_DASH = "—"


@dataclass(frozen=True)
class PanelRow:
    id: int
    name: str
    digits: str  # ••last4, with the bank's newer digits when they differ; empty without any
    joint: bool
    balance: str  # formatted, or an em dash when no ledger balance has been imported
    spark: str | None  # SVG polyline points, or None with fewer than two balance points
    spark_color: str  # a design token, from the direction of the sparkline
    warning: bool  # the balance check has issues
    active: bool  # this is the account whose page is open


@dataclass(frozen=True)
class PanelGroup:
    key: str
    label: str
    institution_key: str | None  # set on an institution group, for its logo
    total: str | None  # formatted subtotal, an em dash when no account has a balance, or None
    rows: list[PanelRow]


def panel_grouping(session: Session) -> str:
    """How the panel groups accounts. A missing or unknown stored value means by type."""
    value = get_pref(session, GROUPING_PREF, GROUP_BY_TYPE)
    return value if value in GROUPINGS else GROUP_BY_TYPE


def save_panel_grouping(session: Session, grouping: str) -> None:
    set_pref(session, GROUPING_PREF, grouping)


def account_groups(db: Session, *, active_id: int | None = None) -> list[PanelGroup]:
    """Open accounts in the chosen grouping. Empty when there are no accounts."""
    summaries = account_summaries(db)
    if panel_grouping(db) == GROUP_BY_INSTITUTION:
        return _by_institution(summaries, active_id)
    return _by_type(summaries, active_id)


def _by_type(summaries: list[AccountSummary], active_id: int | None) -> list[PanelGroup]:
    groups: list[PanelGroup] = []
    for total in group_totals(summaries):
        members = [summary for summary in summaries if summary.type == total.type]
        groups.append(
            PanelGroup(
                key=total.type.value,
                label=TYPE_LABELS[total.type],
                institution_key=None,
                total=_money(total.total_cents),
                rows=[_row(summary, active_id) for summary in members],
            )
        )
    return groups


def _by_institution(summaries: list[AccountSummary], active_id: int | None) -> list[PanelGroup]:
    """One group per institution, ordered by name. Accounts keep the order account_summaries gives
    them, so within a group they run by type and then by name."""
    members: dict[int, list[AccountSummary]] = {}
    for summary in summaries:
        members.setdefault(summary.institution_id, []).append(summary)
    ordered = sorted(
        members.values(),
        key=lambda rows: (rows[0].institution.casefold(), rows[0].institution_id),
    )
    return [
        PanelGroup(
            key=f"institution-{rows[0].institution_id}",
            label=rows[0].institution,
            institution_key=rows[0].institution_key,
            total=None,
            rows=[_row(summary, active_id) for summary in rows],
        )
        for rows in ordered
    ]


def sparkline_points(
    values: Sequence[int], *, width: int = SPARK_WIDTH, height: int = SPARK_HEIGHT
) -> str | None:
    """Points for an SVG polyline through the values, left to right, scaled to the box.

    Returns None with fewer than two values. A flat series is drawn along the middle.
    """
    if len(values) < 2:
        return None
    low, high = min(values), max(values)
    span = high - low
    step = width / (len(values) - 1)
    points: list[str] = []
    for index, value in enumerate(values):
        x = index * step
        if span == 0:
            y = height / 2
        else:
            # One pixel of padding top and bottom, so the stroke is not clipped.
            y = 1 + (high - value) / span * (height - 2)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def spark_color(values: Sequence[int]) -> str:
    """The design token for a sparkline: green when it ends higher, red when lower, grey if flat."""
    if len(values) < 2 or values[-1] == values[0]:
        return "var(--color-gray-400)"
    return "var(--color-success)" if values[-1] > values[0] else "var(--color-destructive)"


def _row(summary: AccountSummary, active_id: int | None) -> PanelRow:
    values = [point.cents for point in summary.recent]
    return PanelRow(
        id=summary.id,
        name=summary.name,
        digits=account_digits(summary.last4, summary.alias_last4),
        joint=summary.joint,
        balance=_money(None if summary.latest is None else summary.latest.cents),
        spark=sparkline_points(values),
        spark_color=spark_color(values),
        warning=not summary.status.ok,
        active=summary.id == active_id,
    )


def _money(cents: int | None) -> str:
    """An amount, or an em dash when there is none. Privacy mode blurs the figures in CSS."""
    return EM_DASH if cents is None else format_cents(cents)
