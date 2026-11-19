"""Operational records for external sync runs.

These tables live in core because multiple domain packages (ingest, receipts, and future
connectors) write and query sync runs. SyncRun rows are operational records, not financial ledger
data.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Integer, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from omsync.core.db import Base


class SyncStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    needs_user_action = "needs_user_action"
    cancelled = "cancelled"


class SyncKind(StrEnum):
    bank = "bank"
    receipt = "receipt"


class SyncRun(Base):
    """An operational record of an external sync attempt."""

    __tablename__ = "sync_run"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SyncKind] = mapped_column(
        Enum(
            SyncKind,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda members: [m.value for m in members],
            name="sync_kind",
        ),
        default=SyncKind.bank,
        server_default="bank",
    )
    target_key: Mapped[str] = mapped_column(String(), default="", server_default="")
    connector_key: Mapped[str] = mapped_column(String())
    source_key: Mapped[str | None] = mapped_column(String(), nullable=True)

    # Legacy / domain-specific columns for bank statement runs
    institution_key: Mapped[str | None] = mapped_column(String(), nullable=True)
    provider_key: Mapped[str | None] = mapped_column(String(), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[SyncStatus] = mapped_column(
        Enum(
            SyncStatus,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda members: [m.value for m in members],
            name="sync_status",
        )
    )
    items_fetched: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    files_fetched: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message: Mapped[str | None] = mapped_column(String(length=500), nullable=True)


@dataclass(frozen=True)
class RecentSync:
    """One SyncRun summary for display. message is already safe to show."""

    id: int
    status: SyncStatus
    started_at: datetime
    finished_at: datetime | None
    items_fetched: int = 0
    message: str | None = None
    kind: SyncKind = SyncKind.bank
    target_key: str = ""
    connector_key: str = ""
    source_key: str | None = None
    files_fetched: int | None = None

    def __post_init__(self) -> None:
        if self.files_fetched is not None and not self.items_fetched:
            object.__setattr__(self, "items_fetched", self.files_fetched)
        elif self.files_fetched is None:
            object.__setattr__(self, "files_fetched", self.items_fetched)


def utc_now() -> datetime:
    """Naive UTC timestamp matching SQLite CURRENT_TIMESTAMP conventions."""
    return datetime.now(UTC).replace(tzinfo=None)


def latest_sync_runs(session: Session, kind: SyncKind | None = None) -> dict[str, RecentSync]:
    """The newest sync run of each target (provider key or vendor profile key)."""
    query = select(func.max(SyncRun.id))
    if kind is not None:
        query = query.where(SyncRun.kind == kind)
    else:
        query = query.where(SyncRun.target_key.is_not(None))
    newest = session.scalars(query.group_by(SyncRun.target_key)).all()
    latest: dict[str, RecentSync] = {}
    for run in session.scalars(select(SyncRun).where(SyncRun.id.in_(newest))).all():
        key = run.target_key or run.provider_key
        if key is not None:
            latest[key] = RecentSync(
                id=run.id,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                items_fetched=run.items_fetched or run.files_fetched,
                message=run.message,
                kind=run.kind,
                target_key=key,
                connector_key=run.connector_key,
                source_key=run.source_key or run.institution_key,
            )
    return latest
