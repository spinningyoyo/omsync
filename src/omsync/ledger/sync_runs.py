"""Sync runs: the operational log of each provider sync.

A run belongs to its provider, which knows its kind, connector, and institution. Runs are not
ledger data and are not batch-tracked. Deleting a provider deletes its runs.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from omsync.core.db import Base
from omsync.ledger.enums import string_enum
from omsync.ledger.sources import Institution, Provider, ProviderKind


class SyncStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    needs_user_action = "needs_user_action"
    cancelled = "cancelled"


class SyncRun(Base):
    """One sync attempt of a provider. items_fetched counts statement files for a bank provider
    and new or changed receipts for a receipt provider."""

    __tablename__ = "sync_run"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("provider.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[SyncStatus] = mapped_column(string_enum(SyncStatus))
    items_fetched: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message: Mapped[str | None] = mapped_column(String(length=500))


@dataclass(frozen=True)
class RecentSync:
    """One run for display, with the provider it belongs to. message is already safe to show."""

    id: int
    provider_key: str
    kind: ProviderKind
    connector_key: str
    status: SyncStatus
    started_at: datetime
    finished_at: datetime | None
    items_fetched: int
    message: str | None


_RECENT = (
    SyncRun.id,
    Provider.key,
    Provider.kind,
    Provider.connector_key,
    SyncRun.status,
    SyncRun.started_at,
    SyncRun.finished_at,
    SyncRun.items_fetched,
    SyncRun.message,
)


def latest_sync_runs(session: Session) -> dict[str, RecentSync]:
    """The newest run of each provider that has one, by provider key."""
    newest = select(func.max(SyncRun.id)).group_by(SyncRun.provider_id)
    rows = session.execute(
        select(*_RECENT)
        .join(Provider, Provider.id == SyncRun.provider_id)
        .where(SyncRun.id.in_(newest))
    ).all()
    return {row[1]: RecentSync(*row) for row in rows}


def recent_sync_runs(session: Session, institution_key: str, limit: int) -> tuple[RecentSync, ...]:
    """The last runs of an institution's bank providers, newest first."""
    rows = session.execute(
        select(*_RECENT)
        .join(Provider, Provider.id == SyncRun.provider_id)
        .join(Institution, Institution.id == Provider.institution_id)
        .where(Institution.key == institution_key)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(limit)
    ).all()
    return tuple(RecentSync(*row) for row in rows)


def provider_names(session: Session) -> dict[str, str]:
    """Every provider's display name, by key."""
    return dict(session.execute(select(Provider.key, Provider.name)).all())
