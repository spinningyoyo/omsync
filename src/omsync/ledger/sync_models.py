"""Sync runs: one row per sync, the log of what a connector fetched and how the sync ended.

SyncRun rows are operational records, not ledger data. A sync writes them directly, not as
batches, so the log entry of a sync stays even when the import it caused is undone. This is
the one exception to "bulk writes go through core.batches". The message holds no password,
token, or account number. Connectors are trusted to keep their messages that way.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from omsync.core.db import Base
from omsync.ledger.enums import string_enum


class SyncStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    needs_user_action = "needs_user_action"


class SyncRun(Base):
    """One sync of one provider. Written when it starts, and finished when it ends.

    provider_key is None only for runs logged before providers existed.
    """

    __tablename__ = "sync_run"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_key: Mapped[str]
    provider_key: Mapped[str | None]
    connector_key: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[SyncStatus] = mapped_column(string_enum(SyncStatus))
    files_fetched: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message: Mapped[str | None] = mapped_column(String(200))
