"""View data for the account page: the chart's points, the sync card's rows, and the nicknames
card's rows. The numbers come from omsync.ledger.accounts_view and omsync.ledger.contacts. This
module only formats them.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.ingest.connectors.base import ConnectorError
from omsync.ingest.connectors.registry import get_connector
from omsync.ingest.institutions import RecentSync
from omsync.ingest.models import SyncStatus
from omsync.ledger.accounts_view import BalancePoint
from omsync.ledger.contacts import list_nicknames
from omsync.web.formats import long_day

SYNC_STATUS: dict[SyncStatus, tuple[str, str]] = {
    SyncStatus.running: ("Running", "indigo"),
    SyncStatus.succeeded: ("Succeeded", "green"),
    SyncStatus.failed: ("Failed", "red"),
    SyncStatus.needs_user_action: ("Needs you", "amber"),
    SyncStatus.cancelled: ("Cancelled", "gray"),
}


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


def chart_points(points: Sequence[BalancePoint]) -> list[dict[str, object]]:
    """The chart's data: each point has its ISO date, a label, its cents, and its amount text."""
    return [
        {
            "date": point.as_of.isoformat(),
            "label": long_day(point.as_of),
            "cents": point.cents,
            "formatted": format_cents(point.cents),
        }
        for point in points
    ]


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
