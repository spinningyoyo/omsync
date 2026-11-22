"""Institutions, the accounts they hold, how statement files name those accounts, and providers.

An account's identity is its row here, not its last four digits. Several numbers can map to one
account through AccountAlias: statement account IDs, as when a card is reissued, and card numbers
that only receipts show.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    ColumnElement,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    ScalarSelect,
    String,
    UniqueConstraint,
    column,
    exists,
    false,
    func,
    or_,
    select,
    table,
    true,
)
from sqlalchemy.orm import Mapped, aliased, column_property, mapped_column

from omsync.core.db import Base
from omsync.ledger.enums import string_enum


class AccountType(StrEnum):
    checking = "checking"
    savings = "savings"
    credit_card = "credit_card"
    brokerage = "brokerage"
    other = "other"


class ProviderKind(StrEnum):
    bank = "bank"
    receipt = "receipt"


class Institution(Base):
    """A bank, card issuer, or brokerage that holds accounts."""

    __tablename__ = "institution"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Account(Base):
    """One financial account at an institution. last4 is a display label only. rental_label is set
    when the account is a rental property: the property's name in the rental report."""

    __tablename__ = "account"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institution.id"), index=True)
    name: Mapped[str]
    type: Mapped[AccountType] = mapped_column(string_enum(AccountType))
    last4: Mapped[str | None] = mapped_column(String(4))
    joint: Mapped[bool] = mapped_column(default=False, server_default=false())
    closed: Mapped[bool] = mapped_column(default=False, server_default=false())
    rental_label: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AccountAlias(Base):
    """Maps a number to an account. Several numbers may map to one account. A receipts_only
    number is one the bank never shows, such as a phone wallet's card number: receipts match it,
    and sync never asks the bank for it."""

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
    receipts_only: Mapped[bool] = mapped_column(default=False, server_default=false())


# The statement tables, by name only: ledger.statements imports this module.
_statement_file = table(
    "statement_file",
    column("id", Integer),
    column("account_id", Integer),
    column("statement_account_id", String),
    column("period_end", Date),
)
_statement_row = table(
    "statement_row", column("statement_file_id", Integer), column("posted_on", Date)
)


def current_alias_id(account_id: Any) -> ScalarSelect[int]:
    """The id of the alias the bank knows the account by now, or None without an alias.

    Aliases are compared by their last four digits, which is what a bank's site shows. The digits
    whose statements reach the latest date win; a file's date is its period end, or its latest
    posted row when it has no period end. Digits no statement carries yet are a new card the person
    has told omsync about, so they win over any date. Among aliases with the winning digits, the
    one whose own statements are latest is chosen, then the newest row. The order rows were added
    in says nothing about when the bank used a number, so a merge of old files cannot change it.
    A receipts_only alias is never the current one, because the bank never shows it."""
    candidate = aliased(AccountAlias)
    file_date = func.coalesce(
        _statement_file.c.period_end,
        select(func.max(_statement_row.c.posted_on))
        .where(_statement_row.c.statement_file_id == _statement_file.c.id)
        .scalar_subquery(),
    )
    latest = select(func.max(file_date)).where(_statement_file.c.account_id == candidate.account_id)
    digits_latest = latest.where(
        func.substr(_statement_file.c.statement_account_id, -4)
        == func.substr(candidate.statement_account_id, -4)
    ).scalar_subquery()
    own_latest = latest.where(
        _statement_file.c.statement_account_id == candidate.statement_account_id
    ).scalar_subquery()
    # SQLite sorts NULL lowest, so a descending date puts aliases without statements last.
    return (
        select(candidate.id)
        .where(candidate.account_id == account_id, candidate.receipts_only.is_(False))
        .order_by(
            digits_latest.is_(None).desc(),
            digits_latest.desc(),
            own_latest.desc(),
            candidate.id.desc(),
        )
        .limit(1)
        .correlate_except(candidate)
        .scalar_subquery()
    )


# The last four of the account's current alias (see current_alias_id): the digits its bank shows
# now, which differ from the displayed last4 once a card is reissued. None without an alias of four
# or more.
Account.alias_last4 = column_property(
    select(func.substr(AccountAlias.statement_account_id, -4))
    .where(
        AccountAlias.id == current_alias_id(Account.id),
        func.length(AccountAlias.statement_account_id) >= 4,
    )
    .correlate_except(AccountAlias)
    .scalar_subquery()
)


def has_number(last4: str) -> ColumnElement[bool]:
    """Whether an account is known by these last four digits: its own, or any alias's."""
    return or_(
        Account.last4 == last4,
        exists().where(
            AccountAlias.account_id == Account.id,
            func.substr(AccountAlias.statement_account_id, -4) == last4,
        ),
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
    """How omsync fetches statements or receipts: a bank connector or a receipt vendor, and its
    vault login. name is the label its owner chose.

    An institution may have several bank providers, each fetching only the accounts it owns (see
    ProviderAccount). A bank provider has an institution. A receipt provider has none and no
    accounts. Keys are unique across providers.
    """

    __tablename__ = "provider"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(unique=True)
    kind: Mapped[ProviderKind] = mapped_column(string_enum(ProviderKind))
    name: Mapped[str]
    connector_key: Mapped[str]
    institution_id: Mapped[int | None] = mapped_column(ForeignKey("institution.id"), index=True)
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
