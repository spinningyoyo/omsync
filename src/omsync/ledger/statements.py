"""Statement files as received, their rows and balances, unchanged after import.

Statement rows are the evidence that import matches against transactions. They are never
updated after insert: a correction is a new transaction change, not an edit to the row.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    false,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from omsync.core.db import Base
from omsync.ledger.enums import string_enum
from omsync.ledger.sources import Account, Institution
from omsync.ledger.transactions import MatchKind, TransactionRow

# Ids per query, so a long list stays under SQLite's bound-parameter limit.
_LOOKUP_CHUNK = 900


class BalanceKind(StrEnum):
    ledger = "ledger"
    available = "available"


class StatementFile(Base):
    """One file covering one account over a date range, identified by its SHA-256."""

    __tablename__ = "statement_file"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    format_key: Mapped[str]
    original_name: Mapped[str]
    stored_path: Mapped[str]
    statement_account_id: Mapped[str]
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"), index=True)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    imported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StatementRow(Base):
    """One line exactly as a statement file reported it. Never updated after insert."""

    __tablename__ = "statement_row"
    __table_args__ = (
        UniqueConstraint("statement_file_id", "position", name="uq_statement_row_file_position"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_file_id: Mapped[int] = mapped_column(ForeignKey("statement_file.id"), index=True)
    position: Mapped[int]
    posted_on: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int]
    payee_raw: Mapped[str]
    memo_raw: Mapped[str | None]
    fitid: Mapped[str | None] = mapped_column(index=True)
    # Whether the format vouched for the FITID when the row was imported. A stored FITID alone does
    # not say this: some formats keep IDs that are not stable, and they are stored too.
    fitid_trusted: Mapped[bool] = mapped_column(server_default=false())
    fingerprint: Mapped[str] = mapped_column(index=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON)


class StatementBalance(Base):
    """The balance an institution reported for an account as of a date."""

    __tablename__ = "statement_balance"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"), index=True)
    statement_file_id: Mapped[int] = mapped_column(ForeignKey("statement_file.id"), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    balance_cents: Mapped[int]
    kind: Mapped[BalanceKind] = mapped_column(string_enum(BalanceKind))


@dataclass(frozen=True)
class SourceText:
    """The NAME and MEMO a statement row reported, exactly as read. Never edited after import.

    institution_key and format_key say who wrote them and how the file was read, which decide
    whether the MEMO continues the NAME.
    """

    name: str
    memo: str | None
    institution_key: str | None
    format_key: str


def source_texts(session: Session, transaction_ids: Iterable[int]) -> dict[int, SourceText]:
    """Return the source text of each transaction that a statement row created or matched.

    A transaction can have several rows, one per statement that described it. The row that
    created the transaction is preferred, then the lowest row ID, so the choice is the same on
    every run. A transaction with no statement row is absent from the result.
    """
    ids = list(dict.fromkeys(transaction_ids))
    found: dict[int, SourceText] = {}
    for start in range(0, len(ids), _LOOKUP_CHUNK):
        chunk = ids[start : start + _LOOKUP_CHUNK]
        rows = session.execute(
            select(
                TransactionRow.transaction_id,
                StatementRow.payee_raw,
                StatementRow.memo_raw,
                Institution.key,
                StatementFile.format_key,
            )
            .join(StatementRow, StatementRow.id == TransactionRow.statement_row_id)
            .join(StatementFile, StatementFile.id == StatementRow.statement_file_id)
            .join(Account, Account.id == StatementFile.account_id)
            .join(Institution, Institution.id == Account.institution_id)
            .where(TransactionRow.transaction_id.in_(chunk))
            .order_by(
                TransactionRow.transaction_id,
                TransactionRow.match_kind != MatchKind.created,
                StatementRow.id,
            )
        ).all()
        for txn_id, name, memo, institution_key, format_key in rows:
            found.setdefault(
                txn_id,
                SourceText(
                    name=name, memo=memo, institution_key=institution_key, format_key=format_key
                ),
            )
    return found
