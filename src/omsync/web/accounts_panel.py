"""The accounts panel's view: open accounts grouped by type or by institution. Type groups carry a
subtotal, its latest balance, a sparkline, a joint badge, and a warning when its balance check has
issues. Institution groups carry no subtotal, because netting a card's debt against a checking
balance means nothing. Closed accounts follow in a last, collapsed group with only their names, as
YNAB and Actual Budget keep them, so a closed account can be found and reopened from its page.

The sparkline is an inline SVG polyline computed here, so the panel needs no JavaScript. It and the
percent beside it cover the 30 days up to each account's latest balance, as Sure's sidebar shows a
recent trend under the balance.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.ledger.accounts_view import (
    TREND_DAYS,
    AccountSummary,
    BalancePoint,
    account_summaries,
    closed_accounts,
    group_totals,
)
from omsync.ledger.models import AccountType
from omsync.ledger.preferences import get_pref, set_pref
from omsync.ledger.sources import Account, account_digits

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
    digits: str  # ••last4 of the number the bank knows the account by now; empty without any
    digits_all: str  # every number, as the account page shows them, or empty when only one
    joint: bool
    balance: str  # formatted, or an em dash when no ledger balance has been imported
    spark: str | None  # SVG polyline points, or None with fewer than two balance points
    spark_color: str  # a design token, from the direction of the sparkline
    change: str | None  # the balance's change over the trend as a percent, or None without one
    change_label: str  # what change measures, for the tooltip and screen readers
    warning: bool  # the balance check has issues
    active: bool  # this is the account whose page is open


@dataclass(frozen=True)
class PanelGroup:
    key: str
    label: str
    institution_key: str | None  # set on an institution group, for its logo
    total: str | None  # formatted subtotal, an em dash when no account has a balance, or None
    rows: list[PanelRow]
    closed: bool = False  # the group of closed accounts, drawn collapsed


def panel_grouping(session: Session) -> str:
    """How the panel groups accounts. A missing or unknown stored value means by type."""
    value = get_pref(session, GROUPING_PREF, GROUP_BY_TYPE)
    return value if value in GROUPINGS else GROUP_BY_TYPE


def save_panel_grouping(session: Session, grouping: str) -> None:
    set_pref(session, GROUPING_PREF, grouping)


def account_groups(db: Session, *, active_id: int | None = None) -> list[PanelGroup]:
    """Open accounts in the chosen grouping, then the closed group when any account is closed.
    Empty when there are no accounts."""
    summaries = account_summaries(db)
    if panel_grouping(db) == GROUP_BY_INSTITUTION:
        groups = _by_institution(summaries, active_id)
    else:
        groups = _by_type(summaries, active_id)
    closed = [_closed_row(account, active_id) for account in closed_accounts(db)]
    if closed:
        groups.append(PanelGroup("closed", "Closed", None, None, closed, closed=True))
    return groups


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
    values: Sequence[int],
    *,
    positions: Sequence[float] | None = None,
    width: int = SPARK_WIDTH,
    height: int = SPARK_HEIGHT,
) -> str | None:
    """Points for an SVG polyline through the values, left to right, scaled to the box.

    positions places each value across the box, from 0 to 1; without them the values are evenly
    spaced. Returns None with fewer than two values. A flat series is drawn along the middle.
    """
    if len(values) < 2:
        return None
    low, high = min(values), max(values)
    span = high - low
    if positions is None:
        positions = [index / (len(values) - 1) for index in range(len(values))]
    points: list[str] = []
    for value, position in zip(values, positions, strict=True):
        x = position * width
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


def trend_change(values: Sequence[int]) -> str | None:
    """The change from the first value to the last, as a percent of the first's size, signed as
    the balance moved: a card that owes more goes down, as its negative balance does. None with
    fewer than two values or a first value of zero, where a percent means nothing."""
    if len(values) < 2 or values[0] == 0:
        return None
    percent = (values[-1] - values[0]) / abs(values[0]) * 100
    text = f"{abs(percent):.0f}" if abs(percent) >= 100 else f"{abs(percent):.1f}"
    if text in ("0", "0.0"):
        return "0.0%"
    return f"{'+' if percent > 0 else '-'}{text}%"


def _positions(points: Sequence[BalancePoint]) -> list[float]:
    """Each point's place across the trend's days. The balance carried in from before them is
    drawn at the start."""
    end = points[-1].as_of
    start = end - timedelta(days=TREND_DAYS)
    return [max((point.as_of - start).days, 0) / TREND_DAYS for point in points]


def _row(summary: AccountSummary, active_id: int | None) -> PanelRow:
    values = [point.cents for point in summary.recent]
    since = summary.recent[0].as_of if summary.recent else None
    return PanelRow(
        id=summary.id,
        name=summary.name,
        digits=_current_digits(summary.last4, summary.alias_last4),
        digits_all=_all_digits(summary.last4, summary.alias_last4),
        joint=summary.joint,
        balance=_money(None if summary.latest is None else summary.latest.cents),
        spark=sparkline_points(values, positions=_positions(summary.recent)) if values else None,
        spark_color=spark_color(values),
        change=trend_change(values),
        change_label=f"Change since {since:%b} {since.day}" if since else "",
        warning=not summary.status.ok,
        active=summary.id == active_id,
    )


def _closed_row(account: Account, active_id: int | None) -> PanelRow:
    return PanelRow(
        id=account.id,
        name=account.name,
        digits=_current_digits(account.last4, account.alias_last4),
        digits_all=_all_digits(account.last4, account.alias_last4),
        joint=bool(account.joint),
        balance="",
        spark=None,
        spark_color="",
        change=None,
        change_label="",
        warning=False,
        active=account.id == active_id,
    )


def _current_digits(last4: str | None, alias_last4: str | None) -> str:
    """The number the bank knows the account by now: the current alias's, else the account's own.
    The panel shows only this one, so a reissued card's row stays short."""
    return account_digits(alias_last4 or last4) if last4 else ""


def _all_digits(last4: str | None, alias_last4: str | None) -> str:
    """Both numbers, for the hover, when the current one differs from the account's own."""
    full = account_digits(last4, alias_last4)
    return full if full != _current_digits(last4, alias_last4) else ""


def _money(cents: int | None) -> str:
    """An amount, or an em dash when there is none. Privacy mode blurs the figures in CSS."""
    return EM_DASH if cents is None else format_cents(cents)
