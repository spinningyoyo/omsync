"""Materialized receipt models stored in SQLite.

Receipts, items, and tenders mirror statement files and statement rows.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    false,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship, selectinload

from omsync.core.db import Base
from omsync.ledger.models import Provider
from omsync.receipts.model import Receipt, ReceiptItem, Tender


class ReceiptRecord(Base):
    """One receipt stored in SQLite as reported by a vendor. It belongs to the provider that
    fetched it, and one order or barcode is one row, whichever provider asks for it."""

    __tablename__ = "receipt"
    __table_args__ = (
        UniqueConstraint("vendor_key", "receipt_id", name="uq_receipt_vendor_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("provider.id"), index=True)
    vendor_key: Mapped[str] = mapped_column(String(32), index=True)
    receipt_id: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[str] = mapped_column(String(64))
    purchased_on: Mapped[date] = mapped_column(Date, index=True)
    total_cents: Mapped[int] = mapped_column(Integer)
    tax_cents: Mapped[int] = mapped_column(Integer)
    is_refund: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    refund_of: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ship_dates: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    stored_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    provider: Mapped[Provider] = relationship()
    items: Mapped[list["ReceiptItemRecord"]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
        order_by="ReceiptItemRecord.position",
    )
    tenders: Mapped[list["ReceiptTenderRecord"]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
    )

    def to_domain(self) -> Receipt:
        """Convert SQLite record to domain Receipt dataclass."""
        return Receipt(
            vendor_key=self.vendor_key,
            receipt_id=self.receipt_id,
            revision=self.revision,
            purchased_on=self.purchased_on,
            account=self.provider.key,
            items=tuple(
                ReceiptItem(
                    position=item.position,
                    description=item.description,
                    amount_cents=item.amount_cents,
                    taxable=item.taxable,
                    quantity=item.quantity,
                    code=item.code,
                )
                for item in self.items
            ),
            tax_cents=self.tax_cents,
            total_cents=self.total_cents,
            tenders=tuple(
                Tender(
                    kind=t.kind,
                    last4=t.last4,
                    amount_cents=t.amount_cents,
                )
                for t in self.tenders
            ),
            is_refund=self.is_refund,
            refund_of=self.refund_of,
            ship_dates=tuple(
                date.fromisoformat(d) if isinstance(d, str) else d for d in (self.ship_dates or [])
            ),
        )

    @classmethod
    def from_domain(
        cls,
        receipt: Receipt,
        provider_id: int,
        stored_path: str | None = None,
    ) -> "ReceiptRecord":
        """Build SQLite record from domain Receipt dataclass."""
        rec = cls(
            provider_id=provider_id,
            vendor_key=receipt.vendor_key,
            receipt_id=receipt.receipt_id,
            revision=receipt.revision,
            purchased_on=receipt.purchased_on,
            total_cents=receipt.total_cents,
            tax_cents=receipt.tax_cents,
            is_refund=receipt.is_refund,
            refund_of=receipt.refund_of,
            ship_dates=[d.isoformat() for d in receipt.ship_dates],
            stored_path=stored_path,
        )
        rec.items = [
            ReceiptItemRecord(
                position=item.position,
                description=item.description,
                amount_cents=item.amount_cents,
                taxable=item.taxable,
                quantity=item.quantity,
                code=item.code,
            )
            for item in receipt.items
        ]
        rec.tenders = [
            ReceiptTenderRecord(
                kind=t.kind,
                last4=t.last4,
                amount_cents=t.amount_cents,
            )
            for t in receipt.tenders
        ]
        return rec


class ReceiptItemRecord(Base):
    """One line item on a receipt."""

    __tablename__ = "receipt_item"
    __table_args__ = (
        UniqueConstraint("receipt_id", "position", name="uq_receipt_item_position"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipt.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    description: Mapped[str]
    amount_cents: Mapped[int]
    taxable: Mapped[bool]
    quantity: Mapped[int] = mapped_column(default=1, server_default="1")
    code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    receipt: Mapped[ReceiptRecord] = relationship(back_populates="items")


class ReceiptTenderRecord(Base):
    """How part of a receipt was paid."""

    __tablename__ = "receipt_tender"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipt.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    amount_cents: Mapped[int | None] = mapped_column(nullable=True)

    receipt: Mapped[ReceiptRecord] = relationship(back_populates="tenders")


@dataclass(frozen=True)
class SaveResult:
    """What saving one receipt did. written says it was new or changed. held_by is the ID of the
    other provider that already holds the receipt, in which case nothing was written."""

    written: bool
    held_by: int | None = None


def save_receipt(
    session: Session,
    receipt: Receipt,
    provider_id: int,
    stored_path: str | None = None,
) -> SaveResult:
    """Save or update one receipt of a provider. A receipt that another provider holds is left
    as it is."""
    existing = session.scalars(
        select(ReceiptRecord)
        .options(
            selectinload(ReceiptRecord.items),
            selectinload(ReceiptRecord.tenders),
        )
        .where(
            ReceiptRecord.vendor_key == receipt.vendor_key,
            ReceiptRecord.receipt_id == receipt.receipt_id,
        )
    ).first()
    if existing is not None:
        if existing.provider_id != provider_id:
            return SaveResult(False, held_by=existing.provider_id)
        if existing.revision == receipt.revision and existing.stored_path == stored_path:
            return SaveResult(False)
        existing.revision = receipt.revision
        existing.purchased_on = receipt.purchased_on
        existing.total_cents = receipt.total_cents
        existing.tax_cents = receipt.tax_cents
        existing.is_refund = receipt.is_refund
        existing.refund_of = receipt.refund_of
        existing.ship_dates = [d.isoformat() for d in receipt.ship_dates]
        if stored_path:
            existing.stored_path = stored_path
        existing.items.clear()
        existing.tenders.clear()
        session.flush()
        for item in receipt.items:
            existing.items.append(
                ReceiptItemRecord(
                    position=item.position,
                    description=item.description,
                    amount_cents=item.amount_cents,
                    taxable=item.taxable,
                    quantity=item.quantity,
                    code=item.code,
                )
            )
        for t in receipt.tenders:
            existing.tenders.append(
                ReceiptTenderRecord(
                    kind=t.kind,
                    last4=t.last4,
                    amount_cents=t.amount_cents,
                )
            )
        session.flush()
        return SaveResult(True)

    record = ReceiptRecord.from_domain(receipt, provider_id=provider_id, stored_path=stored_path)
    session.add(record)
    session.flush()
    return SaveResult(True)
