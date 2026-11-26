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
from omsync.ingest.connectors.registry import DropFolder, find_connector
from omsync.ledger.accounts_view import BalancePoint
from omsync.ledger.contacts import list_nicknames
from omsync.ledger.models import ProviderKind, RecentSync, SyncStatus
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
        unit = "receipt" if run.kind is ProviderKind.receipt else "file"
        count = run.items_fetched
        noun = unit if count == 1 else f"{unit}s"
        views.append(
            SyncView(
                status=label,
                tone=tone,
                when=local_time(run.started_at),
                files=f"{count} {noun}",
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
    """What the sync card calls the institution's connector. A provider whose connector is gone
    only takes files you add, like the drop folder."""
    connector = find_connector(connector_key)
    if connector is None or connector.key == DropFolder.key:
        return "Drop folder only"
    return connector.label if enabled else f"{connector.label} (turned off)"


def local_time(moment: datetime) -> str:
    """A stored time is naive UTC. It is shown in this machine's time zone."""
    local = moment.replace(tzinfo=UTC).astimezone()
    return f"{long_day(local)} {local:%H:%M}"
