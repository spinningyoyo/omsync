"""Transactions, their splits, the statement rows they came from, and ambiguous imports.

A transaction keeps its permanent ID for life, however many statement rows describe it.
Its splits always add up to its amount, to the cent. The other side of the money, a contact and
optionally one of its accounts, is stored on a transaction or a split with an origin, as a category
is.
"""

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import JSON, Date, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from omsync.core.db import Base
from omsync.ledger.enums import string_enum


class CategoryOrigin(StrEnum):
    """What set a category. Higher rank means a stronger claim to the value."""

    none = "none"
    keyword = "keyword"
    rule = "rule"
    receipt = "receipt"
    manual = "manual"

    @property
    def rank(self) -> int:
        match self:
            case CategoryOrigin.none:
                return 0
            case CategoryOrigin.keyword | CategoryOrigin.rule:
                return 1
            case CategoryOrigin.receipt:
                return 2
            case CategoryOrigin.manual:
                return 3


def outranks(new: CategoryOrigin, current: CategoryOrigin) -> bool:
    """Return True when new has rank >= current, so a write of origin new may set a value
    that current set. Equal ranks return True, so a keyword may replace a keyword.
    """
    return new.rank >= current.rank


def may_replace(new: CategoryOrigin, current: CategoryOrigin) -> bool:
    """Return True when a category set by origin `new` may overwrite one set by `current`.

    This is the same rule as `outranks`. It is the guard that keeps automation from
    overwriting your choices:

    - manual ("you") is never replaced by a receipt or a keyword;
    - a receipt is never replaced by a keyword or a rule;
    - a keyword or a rule may replace the other, so categorize can rerun;
    - anything may replace none, which means unset.

    A manual choice may still replace a manual one, because that is you changing your mind.
    """
    return outranks(new, current)


class ContactOrigin(StrEnum):
    """What set the other side of a transaction or split. Higher rank means a stronger claim."""

    none = "none"
    recognized = "recognized"
    manual = "manual"

    @property
    def rank(self) -> int:
        match self:
            case ContactOrigin.none:
                return 0
            case ContactOrigin.recognized:
                return 1
            case ContactOrigin.manual:
                return 2


def may_replace_contact(new: ContactOrigin, current: ContactOrigin) -> bool:
    """Return True when an other side set by origin `new` may overwrite one set by `current`.

    Recognition never replaces your choice, may replace an earlier recognition so it can rerun,
    and may replace none. A manual choice may replace any, because that is you changing your mind.
    """
    return new.rank >= current.rank


class Transaction(Base):
    """One real movement of money on one account. The ID is never reused, even after delete."""

    __tablename__ = "transaction"
    # Without AUTOINCREMENT, SQLite can hand a deleted transaction's ID to a new row.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)
    posted_on: Mapped[date] = mapped_column(Date, index=True)
    amount_cents: Mapped[int]
    payee: Mapped[str]
    payee_raw: Mapped[str]
    memo: Mapped[str | None]
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), index=True)
    category_origin: Mapped[CategoryOrigin] = mapped_column(
        string_enum(CategoryOrigin),
        default=CategoryOrigin.none,
        server_default=CategoryOrigin.none.value,
    )
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contact.id"), index=True)
    contact_account_id: Mapped[int | None] = mapped_column(ForeignKey("contact_account.id"))
    contact_origin: Mapped[ContactOrigin] = mapped_column(
        string_enum(ContactOrigin),
        default=ContactOrigin.none,
        server_default=ContactOrigin.none.value,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    splits: Mapped[list["Split"]] = relationship(
        back_populates="transaction",
        cascade="all, delete-orphan",
        order_by="Split.position",
    )


class MatchKind(StrEnum):
    """How a statement row was matched to its transaction."""

    created = "created"
    fitid = "fitid"
    fingerprint = "fingerprint"
    manual = "manual"


class TransactionRow(Base):
    """Maps one statement row to one transaction. A row has at most one transaction."""

    __tablename__ = "transaction_row"

    statement_row_id: Mapped[int] = mapped_column(ForeignKey("statement_row.id"), primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transaction.id"), index=True)
    match_kind: Mapped[MatchKind] = mapped_column(string_enum(MatchKind))


class Split(Base):
    """A part of a transaction with its own amount, category, and origin."""

    __tablename__ = "split"
    __table_args__ = (
        UniqueConstraint("transaction_id", "position", name="uq_split_transaction_position"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transaction.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    amount_cents: Mapped[int]
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), index=True)
    category_origin: Mapped[CategoryOrigin] = mapped_column(
        string_enum(CategoryOrigin),
        default=CategoryOrigin.none,
        server_default=CategoryOrigin.none.value,
    )
    memo: Mapped[str | None]
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contact.id"), index=True)
    contact_account_id: Mapped[int | None] = mapped_column(ForeignKey("contact_account.id"))
    contact_origin: Mapped[ContactOrigin] = mapped_column(
        string_enum(ContactOrigin),
        default=ContactOrigin.none,
        server_default=ContactOrigin.none.value,
    )

    transaction: Mapped[Transaction] = relationship(back_populates="splits")


class ReviewStatus(StrEnum):
    open = "open"
    resolved = "resolved"


class ImportReview(Base):
    """A statement row whose match was ambiguous, waiting for you to decide."""

    __tablename__ = "import_review"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_row_id: Mapped[int] = mapped_column(
        ForeignKey("statement_row.id"), unique=True, index=True
    )
    reason: Mapped[str]
    candidate_transaction_ids: Mapped[list[int]] = mapped_column(JSON)
    status: Mapped[ReviewStatus] = mapped_column(
        string_enum(ReviewStatus),
        default=ReviewStatus.open,
        server_default=ReviewStatus.open.value,
    )
    resolved_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transaction.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
