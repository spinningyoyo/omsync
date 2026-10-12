"""Batch bookkeeping: one batch per bulk change, its per-row before and after, and the revision.

These tables live in core because every domain package writes batches. Core never imports
ledger; ledger tables refer to `batch` by name in their foreign keys.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Enum, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from omsync.core.db import Base


class BatchState(StrEnum):
    """Lifecycle of a batch. A new batch starts as a preview."""

    previewed = "previewed"
    applied = "applied"
    undone = "undone"
    failed = "failed"


class Batch(Base):
    """One bulk operation: an import, a categorize run, applied receipts, or approved proposals."""

    __tablename__ = "batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    state: Mapped[BatchState] = mapped_column(
        Enum(
            BatchState,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda members: [member.value for member in members],
            name="batch_state",
        ),
        default=BatchState.previewed,
        server_default=BatchState.previewed.value,
    )
    base_revision: Mapped[int]
    idempotency_key: Mapped[str | None] = mapped_column(unique=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime)


class BatchChange(Base):
    """One row changed by a batch, with its values before and after (None when absent)."""

    __tablename__ = "batch_change"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batch.id", ondelete="CASCADE"), index=True)
    table_name: Mapped[str]
    row_id: Mapped[str]
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class DataRevision(Base):
    """Single-row counter that increases on every change, so previews can detect staleness.

    The row is created on first use with id 1. The check constraint allows no other row.
    """

    __tablename__ = "data_revision"
    __table_args__ = (CheckConstraint("id = 1", name="ck_data_revision_single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[int] = mapped_column(default=0, server_default="0")
