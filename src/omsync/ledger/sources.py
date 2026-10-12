"""Institutions, the accounts they hold, how statement files name those accounts, and providers.

An account's identity is its row here, not its last four digits. Several statement account
IDs can map to one account through AccountAlias, as when a card is reissued.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    false,
    func,
    select,
    true,
)
from sqlalchemy.orm import Mapped, column_property, mapped_column

from omsync.core.db import Base
from omsync.ledger.enums import string_enum


class AccountType(StrEnum):
    checking = "checking"
    savings = "savings"
    credit_card = "credit_card"
    brokerage = "brokerage"
    other = "other"


class Institution(Base):
    """A bank, card issuer, or brokerage that holds accounts."""

    __tablename__ = "institution"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Account(Base):
    """One financial account at an institution. last4 is a display label only."""

    __tablename__ = "account"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institution.id"), index=True)
    name: Mapped[str]
    type: Mapped[AccountType] = mapped_column(string_enum(AccountType))
    last4: Mapped[str | None] = mapped_column(String(4))
    joint: Mapped[bool] = mapped_column(default=False, server_default=false())
    closed: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AccountAlias(Base):
    """Maps a statement file's account ID to an account. Several IDs may map to one account."""

    __tablename__ = "account_alias"
    __table_args__ = (
        UniqueConstraint(
            "institution_id",
            "statement_account_id",
            name="uq_account_alias_institution_statement_account",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institution.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)
    statement_account_id: Mapped[str]


# The last four of the account's newest statement account ID: the digits its bank shows now, which
# differ from the displayed last4 once a card is reissued. None without an alias of four or more.
Account.alias_last4 = column_property(
    select(func.substr(AccountAlias.statement_account_id, -4))
    .where(
        AccountAlias.account_id == Account.id,
        func.length(AccountAlias.statement_account_id) >= 4,
    )
    .order_by(AccountAlias.id.desc())
    .limit(1)
    .correlate_except(AccountAlias)
    .scalar_subquery()
)


def account_digits(last4: str | None, alias_last4: str | None = None) -> str:
    """An account's masked digits for display, as ••1234, or ••1234 · alias ••5678 when the bank
    now knows it by other digits. Empty when the account has no last four."""
    if not last4:
        return ""
    if alias_last4 and alias_last4 != last4:
        return f"••{last4} · alias ••{alias_last4}"
    return f"••{last4}"


class Provider(Base):
    """How omsync fetches statements for one institution: a bank connector and its vault login.

    An institution may have several providers, each fetching only the accounts it owns (see
    ProviderAccount). Keys are unique across providers, as institution keys are across institutions.
    """

    __tablename__ = "provider"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(unique=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institution.id"), index=True)
    connector_key: Mapped[str]
    vault_item_id: Mapped[str | None]
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())


class ProviderAccount(Base):
    """A provider fetches statements for this account. An account has at most one provider."""

    __tablename__ = "provider_account"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("provider.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), unique=True, index=True)
