"""The statement format protocol and the parsed types every format returns.

Parsed rows and balances are plain values. Nothing here touches the database; import
matches them against transactions in a later step. Messages in FormatError name the file
and the problem, never the contents of a row.
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

AccountType = Literal["checking", "savings", "credit_card", "brokerage"]
BalanceKind = Literal["ledger", "available"]


class FormatError(Exception):
    """A statement file cannot be read. The message names the file, never its contents."""


@dataclass(frozen=True)
class ParsedRow:
    """One statement row. position is 1-based over the rows in file order."""

    position: int
    posted_on: date
    amount_cents: int
    payee_raw: str
    memo_raw: str | None
    fitid: str | None
    raw: dict[str, str]


@dataclass(frozen=True)
class ParsedBalance:
    as_of: date
    balance_cents: int
    kind: BalanceKind


@dataclass(frozen=True)
class Statement:
    format_key: str
    sha256: str
    statement_account_id: str
    account_type_hint: AccountType | None
    period_start: date | None
    period_end: date | None
    rows: tuple[ParsedRow, ...]
    balances: tuple[ParsedBalance, ...]


@dataclass(frozen=True)
class MatchEvidence:
    """What import matching may use to identify a row's transaction.

    fitid is kept even when it is not trusted, so a reviewer can see it. fingerprint is
    computed here from the statement account ID, but import recomputes it from the omsync
    account, so matching uses only fitid and fitid_trusted from this value.
    """

    fitid: str | None
    fitid_trusted: bool
    fingerprint: str


def normalize_payee(text: str) -> str:
    """Uppercase, collapse runs of whitespace to one space, and strip the ends."""
    return " ".join(text.split()).upper()


def fingerprint(
    account_key: str,
    posted_on: date,
    amount_cents: int,
    payee_raw: str,
) -> str:
    """SHA-256 over account key, date, amount, and normalized payee.

    account_key is the omsync account, not the statement account ID, so every alias of one
    account produces the same fingerprints. Import computes fingerprints itself, with the
    account it resolved, and does not use the one a format's evidence carries.

    Occurrence numbering for identical rows is not done here; import matching does it.
    """
    key = f"{account_key}|{posted_on.isoformat()}|{amount_cents}|{normalize_payee(payee_raw)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# Chase cuts NAME at this many characters and continues the description in MEMO.
NAME_CUT_LENGTH = 32


def join_name_and_memo(name: str, memo: str | None) -> str:
    """The whole description of a row whose MEMO continues its NAME.

    A NAME of exactly NAME_CUT_LENGTH characters was cut mid-token, so the MEMO follows with no
    space. A shorter NAME ended where the bank wrote it, so one space separates them. The MEMO is
    left out when the NAME already contains it.
    """
    text = name.strip()
    note = (memo or "").strip()
    if not note or note.lower() in text.lower():
        return text
    separator = "" if len(name) == NAME_CUT_LENGTH else " "
    return f"{text}{separator}{note}"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def evidence_for(statement: Statement, row: ParsedRow, *, fitid_trusted: bool) -> MatchEvidence:
    """Match evidence for a row: its FITID (trusted or not) and a fingerprint.

    The fingerprint here is keyed by the statement account ID. Import recomputes it from the
    omsync account, so only the FITID and its trust flag matter to matching.
    """
    return MatchEvidence(
        fitid=row.fitid,
        fitid_trusted=fitid_trusted,
        fingerprint=fingerprint(
            statement.statement_account_id, row.posted_on, row.amount_cents, row.payee_raw
        ),
    )


class StatementFormat(Protocol):
    """Rules for reading one kind of statement file, whatever fetched it."""

    key: str

    def detect(self, path: Path) -> bool:
        """Return True if the file looks like this format. Never raises for bad content."""
        ...

    def read(self, path: Path, *, statement_account_id: str | None = None) -> Statement:
        """Parse the whole file. Raises FormatError if it cannot be read exactly."""
        ...

    def evidence(self, statement: Statement, row: ParsedRow) -> MatchEvidence:
        """Match evidence for one row of a statement this format read. Import uses its FITID
        and trust flag, and recomputes the fingerprint from the omsync account."""
        ...
