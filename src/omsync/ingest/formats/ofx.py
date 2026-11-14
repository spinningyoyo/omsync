"""OFX bank and credit-card statements, OFX 1.x (SGML) and 2.x (XML), read with ofxparse.

Amounts are exact: ofxparse's Decimal becomes cents through to_cents(str(...)), never a
float. Signs are OFX's: negative means money left the account, or a charge on a card.

Dates are the calendar digits of each OFX date tag (the first eight digits, in the
institution's own time zone), read from the file itself. ofxparse converts times to UTC,
which moves a transaction stamped late in the evening to the next day, so its dates are
not used. Date tags are matched to transactions in file order; a file where the two
disagree is rejected.
"""

import io
import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ofxparse import AccountType as OfxAccountType
from ofxparse import OfxParser

from omsync.core.money import to_cents
from omsync.ingest.formats.base import (
    AccountType,
    BalanceKind,
    FormatError,
    MatchEvidence,
    ParsedBalance,
    ParsedRow,
    Statement,
    evidence_for,
    sha256_of,
)

# Tags whose first eight digits are a calendar date. Block tags say which balance a DTASOF
# belongs to: it is the one that follows its LEDGERBAL or AVAILBAL tag.
_DATE_TAG = re.compile(
    rb"<(?P<tag>LEDGERBAL|AVAILBAL|DTPOSTED|DTSTART|DTEND|DTASOF)>(?P<value>[^<\r\n]*)",
    re.IGNORECASE,
)
_BALANCE_BLOCKS: dict[str, BalanceKind] = {"LEDGERBAL": "ledger", "AVAILBAL": "available"}
_BANK_TYPES: dict[str, AccountType] = {
    "CHECKING": "checking",
    "SAVINGS": "savings",
    "CREDITLINE": "credit_card",
}


@dataclass(frozen=True)
class _Dates:
    posted: tuple[tuple[str, date], ...]  # (raw DTPOSTED text, calendar date), file order
    start: date | None
    end: date | None
    as_of: dict[BalanceKind, date]


class OfxFormat:
    """OFX statements. Pass fitid_trusted=False for an institution whose FITIDs change, and
    memo_continues_name=True for one that cuts NAME short and carries the rest in MEMO."""

    key = "ofx"

    def __init__(self, fitid_trusted: bool = True, memo_continues_name: bool = False) -> None:
        self.fitid_trusted = fitid_trusted
        self.memo_continues_name = memo_continues_name

    def detect(self, path: Path) -> bool:
        with path.open("rb") as handle:
            head = handle.read(2048)
        return b"OFXHEADER" in head or b"<OFX>" in head

    def read(self, path: Path, *, statement_account_id: str | None = None) -> Statement:
        data = path.read_bytes()
        account = _only_account(_parse(data, path), path)
        statement_data = account.statement
        if statement_data is None:
            raise FormatError(f"{path.name}: no statement in the file")

        account_id = account.account_id.strip()
        if not account_id:
            raise FormatError(f"{path.name}: the account has no ACCTID")
        if statement_account_id is not None and statement_account_id != account_id:
            raise FormatError(f"{path.name}: the statement account ID does not match the file")

        dates = _scan_dates(data, path)
        transactions = statement_data.transactions
        if len(transactions) != len(dates.posted):
            raise FormatError(f"{path.name}: date tags do not match the transactions")
        rows = _rows(transactions, dates, path)
        balances = _balances(statement_data, dates, path)

        return Statement(
            format_key=self.key,
            sha256=sha256_of(data),
            statement_account_id=account_id,
            account_type_hint=_account_type(account),
            period_start=dates.start,
            period_end=dates.end,
            rows=tuple(rows),
            balances=tuple(balances),
        )

    def evidence(self, statement: Statement, row: ParsedRow) -> MatchEvidence:
        """The FITID and whether this institution's FITIDs are trusted. Import recomputes the
        fingerprint from the omsync account, so the one returned here is not used."""
        return evidence_for(statement, row, fitid_trusted=self.fitid_trusted)


def _parse(data: bytes, path: Path) -> Any:
    try:
        with warnings.catch_warnings():
            # ofxparse 0.21 parses OFX 2.x XML with the HTML parser and calls the
            # deprecated bs4 findAll. Neither affects the result; hide them here only.
            warnings.filterwarnings("ignore", message=".*HTML parser to parse an XML")
            warnings.filterwarnings("ignore", message="Call to deprecated method findAll")
            return OfxParser.parse(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - ofxparse raises several types for malformed input
        raise FormatError(f"{path.name}: not a readable OFX file") from None


def _only_account(ofx: Any, path: Path) -> Any:
    if not ofx.accounts:
        raise FormatError(f"{path.name}: no account in the file")
    if len(ofx.accounts) > 1:
        raise FormatError(f"{path.name}: one account per file")
    account = ofx.accounts[0]
    if account.type not in (OfxAccountType.Bank, OfxAccountType.CreditCard):
        raise FormatError(f"{path.name}: only bank and credit-card statements are supported")
    return account


def _rows(transactions: list[Any], dates: _Dates, path: Path) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    for position, (txn, (posted_text, posted_on)) in enumerate(
        zip(transactions, dates.posted, strict=True), start=1
    ):
        rows.append(
            ParsedRow(
                position=position,
                posted_on=posted_on,
                amount_cents=_cents(txn.amount, path, f"row {position} amount"),
                payee_raw=txn.payee or txn.memo or "",
                memo_raw=txn.memo or None,
                fitid=txn.id or None,
                raw=_raw_fields(txn, posted_text),
            )
        )
    return rows


def _balances(statement_data: Any, dates: _Dates, path: Path) -> list[ParsedBalance]:
    balances: list[ParsedBalance] = []
    ledger = getattr(statement_data, "balance", None)
    if ledger is not None:
        balances.append(_balance(ledger, dates, "ledger", path))
    available = getattr(statement_data, "available_balance", None)
    if available is not None:
        balances.append(_balance(available, dates, "available", path))
    return balances


def _scan_dates(data: bytes, path: Path) -> _Dates:
    posted: list[tuple[str, date]] = []
    start: date | None = None
    end: date | None = None
    as_of: dict[BalanceKind, date] = {}
    block: BalanceKind | None = None
    for match in _DATE_TAG.finditer(data):
        tag = match.group("tag").decode("ascii").upper()
        value = match.group("value").decode("ascii", "replace").strip()
        if tag in _BALANCE_BLOCKS:
            block = _BALANCE_BLOCKS[tag]
        elif tag == "DTASOF":
            if block is not None:
                as_of[block] = _calendar_date(value, path, tag)
                block = None
        elif tag == "DTPOSTED":
            posted.append((value, _calendar_date(value, path, tag)))
        elif tag == "DTSTART" and start is None:
            start = _calendar_date(value, path, tag)
        elif tag == "DTEND" and end is None:
            end = _calendar_date(value, path, tag)
    return _Dates(posted=tuple(posted), start=start, end=end, as_of=as_of)


def _calendar_date(value: str, path: Path, tag: str) -> date:
    digits = value[:8]
    if not re.fullmatch(r"\d{8}", digits):
        raise FormatError(f"{path.name}: {tag} is not a date")
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        raise FormatError(f"{path.name}: {tag} is not a valid date") from None


def _cents(value: Decimal | None, path: Path, what: str) -> int:
    if value is None:
        raise FormatError(f"{path.name}: {what} is missing")
    try:
        return to_cents(str(value))
    except ValueError:
        raise FormatError(f"{path.name}: {what} is not an amount with two decimal places") from None


def _balance(value: Decimal, dates: _Dates, kind: BalanceKind, path: Path) -> ParsedBalance:
    as_of = dates.as_of.get(kind)
    if as_of is None:
        raise FormatError(f"{path.name}: the {kind} balance has no date")
    return ParsedBalance(
        as_of=as_of,
        balance_cents=_cents(value, path, f"{kind} balance"),
        kind=kind,
    )


def _raw_fields(txn: Any, posted_text: str) -> dict[str, str]:
    """The transaction's fields as text. Empty fields are left out."""
    fields = {
        "DTPOSTED": posted_text,
        "TRNAMT": str(txn.amount),
        "FITID": txn.id,
        "NAME": txn.payee,
        "MEMO": txn.memo,
        "CHECKNUM": txn.checknum,
    }
    return {key: value for key, value in fields.items() if value}


def _account_type(account: Any) -> AccountType | None:
    if account.type == OfxAccountType.CreditCard:
        return "credit_card"
    return _BANK_TYPES.get(str(account.account_type).strip().upper())
