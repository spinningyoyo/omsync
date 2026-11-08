"""View data for the account page: the chart's points, the balance check's issue rows with the
register link for each interval, the sync card's rows, and the nicknames card's rows. The numbers
come from omsync.ledger.accounts_view and omsync.ledger.contacts. This module only formats them and
builds links.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.ingest.balance_check import BalanceIssue, BalanceStatus
from omsync.ingest.connectors.base import ConnectorError
from omsync.ingest.connectors.registry import get_connector
from omsync.ledger.accounts_view import BalancePoint, RecentSync
from omsync.ledger.contacts import list_nicknames
from omsync.ledger.filters import Condition, FilterSpec
from omsync.ledger.models import SyncStatus
from omsync.web.register_state import encode_filter

SYNC_STATUS: dict[SyncStatus, tuple[str, str]] = {
    SyncStatus.running: ("Running", "indigo"),
    SyncStatus.succeeded: ("Succeeded", "green"),
    SyncStatus.failed: ("Failed", "red"),
    SyncStatus.needs_user_action: ("Needs you", "amber"),
    SyncStatus.cancelled: ("Cancelled", "gray"),
}


@dataclass(frozen=True)
class IssueView:
    span: str  # the interval, or the day two balances disagree on
    expected: str
    computed: str
    difference: str
    href: str  # the register, for the transactions in the interval


@dataclass(frozen=True)
class SyncView:
    status: str
    tone: str
    when: str  # local time
    files: str
    message: str | None


@dataclass(frozen=True)
class NicknameRow:
    id: int
    nickname: str
    remove_key: str


def day_label(day: date) -> str:
    return day.strftime("%b %-d, %Y")


def chart_points(points: Sequence[BalancePoint]) -> list[dict[str, object]]:
    """The chart's data: each point has its ISO date, a label, its cents, and its amount text."""
    return [
        {
            "date": point.as_of.isoformat(),
            "label": day_label(point.as_of),
            "cents": point.cents,
            "formatted": format_cents(point.cents),
        }
        for point in points
    ]


def issue_views(account_id: int, status: BalanceStatus) -> list[IssueView]:
    return [_issue_view(account_id, issue) for issue in status.issues]


def _issue_view(account_id: int, issue: BalanceIssue) -> IssueView:
    if issue.from_date == issue.to_date:
        span = f"{day_label(issue.from_date)}, two different balances"
        start = issue.from_date
    else:
        span = f"{day_label(issue.from_date)} to {day_label(issue.to_date)}"
        # The check counts transactions after the earlier balance up to the later one.
        start = issue.from_date + timedelta(days=1)
    spec = FilterSpec(conditions=(Condition("date", "between", (start, issue.to_date)),))
    query = urlencode({"account": account_id, "f": encode_filter(spec)})
    return IssueView(
        span=span,
        expected=format_cents(issue.expected_cents),
        computed=format_cents(issue.computed_cents),
        difference=format_cents(issue.difference_cents),
        href=f"/transactions?{query}",
    )


def sync_views(runs: Sequence[RecentSync]) -> list[SyncView]:
    views: list[SyncView] = []
    for run in runs:
        label, tone = SYNC_STATUS[run.status]
        views.append(
            SyncView(
                status=label,
                tone=tone,
                when=local_time(run.started_at),
                files=f"{run.files_fetched} file" + ("" if run.files_fetched == 1 else "s"),
                message=run.message,
            )
        )
    return views


def nickname_context(db: Session, account_id: int) -> dict[str, object]:
    """The nicknames card's rows: the account's nicknames, and a key for its add form."""
    return {
        "nicknames": tuple(
            NicknameRow(row.id, row.nickname, remove_key=uuid4().hex)
            for row in list_nicknames(db)
            if row.account_id == account_id
        ),
        "nickname_key": uuid4().hex,
    }


def connector_label(connector_key: str | None, enabled: bool) -> str:
    """What the sync card calls the institution's connector."""
    if connector_key is None or connector_key == "drop_folder":
        return "Drop folder only"
    try:
        label = get_connector(connector_key).label
    except ConnectorError:
        label = connector_key
    return label if enabled else f"{label} (turned off)"


def local_time(moment: datetime) -> str:
    """A stored time is naive UTC. It is shown in this machine's time zone."""
    return moment.replace(tzinfo=UTC).astimezone().strftime("%b %-d, %Y %H:%M")
