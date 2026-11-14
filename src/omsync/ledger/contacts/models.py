"""The contact tables, their enums, and the limits on them."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from omsync.core.db import Base
from omsync.ledger.enums import string_enum

MAX_NAME_LENGTH = 80


MAX_NAMES_PER_CONTACT = 10


MAX_PATTERNS_PER_SIDE = 10


MAX_PATTERN_LENGTH = 200


MAX_ACCOUNTS_PER_CONTACT = 20


MAX_NICKNAMES_PER_ACCOUNT = 10


SIDES = ("out", "in")


class NothingChanged(ValueError):
    """An update asked for the values already stored. Nothing was written."""


class ContactKind(StrEnum):
    person = "person"
    business = "business"


class ContactRole(StrEnum):
    owner = "owner"
    co_owner = "co_owner"
    other = "other"


class Contact(Base):
    """A person or business, or you. At most one contact is the owner."""

    __tablename__ = "contact"
    __table_args__ = (
        Index("uq_contact_one_owner", "role", unique=True, sqlite_where=text("role = 'owner'")),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ContactKind] = mapped_column(string_enum(ContactKind))
    role: Mapped[ContactRole] = mapped_column(
        string_enum(ContactRole),
        default=ContactRole.other,
        server_default=ContactRole.other.value,
    )
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ContactName(Base):
    """One name of a contact. A business or a one-word name has only `given`."""

    __tablename__ = "contact_name"
    __table_args__ = (
        UniqueConstraint("contact_id", "position", name="uq_contact_name_contact_position"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contact.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    given: Mapped[str]
    middle: Mapped[str | None]
    family: Mapped[str | None]


class ContactPattern(Base):
    """A regular expression for the wordings of a contact's payments, on one side."""

    __tablename__ = "contact_pattern"
    __table_args__ = (
        UniqueConstraint("contact_id", "side", "pattern", name="uq_contact_pattern_side_pattern"),
        CheckConstraint("side IN ('out', 'in')", name="ck_contact_pattern_side"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contact.id", ondelete="CASCADE"), index=True
    )
    side: Mapped[str]
    pattern: Mapped[str]
    contact_account_id: Mapped[int | None] = mapped_column(ForeignKey("contact_account.id"))


class ContactAccount(Base):
    """An account outside the ledger: one of the owner's untracked accounts, or a contact's own."""

    __tablename__ = "contact_account"
    __table_args__ = (
        CheckConstraint(
            "last4 IS NULL OR (length(last4) = 4 AND last4 NOT GLOB '*[^0-9]*')",
            name="ck_contact_account_last4",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contact.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str]
    last4: Mapped[str | None]


class AccountNickname(Base):
    """The name a payment app shows for a tracked account. Unique per account without regard to
    case: the column compares without case, so the unique constraint does too."""

    __tablename__ = "account_nickname"
    __table_args__ = (
        UniqueConstraint("account_id", "nickname", name="uq_account_nickname_account_nickname"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    nickname: Mapped[str] = mapped_column(String(collation="NOCASE"))
