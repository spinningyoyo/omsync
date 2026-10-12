"""Bank of America CSV statements: a checking layout and a credit-card layout.

BoA no longer offers OFX, so these CSV files are its only statement source.

Sign rule: Amount is kept as BoA wrote it, which already matches OFX. Negative means money
left the account, or a charge on a card. Deposits and card payments are positive.

Checking layout: a summary block comes first, with the lines "Beginning balance as of
MM/DD/YYYY", "Total credits", "Total debits", and "Ending balance as of MM/DD/YYYY". The
header `Date,Description,Amount,Running Bal.` follows. The period is the two printed dates.
The beginning amount is the balance before that day's transactions, so its ledger balance
is dated the day before MM/DD/YYYY. The ending amount is dated MM/DD/YYYY. A row with an
empty Amount, such as the "Beginning balance" row inside the table, is not a transaction.

Running Bal. is checked row by row: the previous balance plus this row's amount must equal
it. The chain starts at the beginning summary amount, or at the in-table "Beginning balance"
row when the summary lacks one. A blank Running Bal. skips the check for that row, and the
computed balance carries forward. A mismatch usually means an unquoted thousands separator
split an amount, or a row is missing, so the file is refused.

Credit-card layout: header `Posted Date,Reference Number,Payee,Address,Amount`. The reference
number becomes fitid as a hint only. BoA's IDs are not guaranteed stable, so fitid_trusted
is always False. A card has no running balance to check, so every doubtful row is refused
rather than guessed at. A card row with an empty Amount is an error, because the card table
has no balance rows. Trailing empty cells are ignored. The amount must be an optional minus,
digits, a dot, and exactly two digits. A row with extra cells is refused if the cell before
Amount, or a piece of the description, looks numeric or is empty.

Shared rules:
- Dates are MM/DD/YYYY. Amounts may be quoted and carry thousands separators and a $ sign.
  BoA quotes an amount with a thousands separator. An unquoted one splits into two cells,
  and a description with a comma looks the same, so the format refuses or checks the row
  rather than guessing.
- A description can hold commas inside nested quotes, which makes its row longer than the
  header. The extra cells are joined back into the description with commas, and stray
  double quotes are dropped from the payee. The raw dict keeps the cells as written.
- statement_account_id is required, because the file does not name its account.
- FormatError messages name the file and the row number, never a row's contents.
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from omsync.core.money import to_cents
from omsync.ingest.formats.base import (
    AccountType,
    FormatError,
    MatchEvidence,
    ParsedBalance,
    ParsedRow,
    Statement,
    evidence_for,
    sha256_of,
)


@dataclass(frozen=True)
class _Layout:
    account_type: AccountType
    header: tuple[str, ...]
    date: int
    description: int
    amount: int
    reference: int | None
    running: int | None


_CHECKING = _Layout(
    account_type="checking",
    header=("Date", "Description", "Amount", "Running Bal."),
    date=0,
    description=1,
    amount=2,
    reference=None,
    running=3,
)
_CARD = _Layout(
    account_type="credit_card",
    header=("Posted Date", "Reference Number", "Payee", "Address", "Amount"),
    date=0,
    description=2,
    amount=4,
    reference=1,
    running=None,
)
_LAYOUTS: dict[tuple[str, ...], _Layout] = {layout.header: layout for layout in (_CHECKING, _CARD)}

_SUMMARY = re.compile(r"(beginning|ending) balance as of (\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
_IN_TABLE_OPENING = re.compile(r"beginning balance", re.IGNORECASE)
# Digits, commas, dots, $ and minus, with at least one digit. Covers "-1", "-$1", and "234.00".
_NUMERIC_CELL = re.compile(r"[0-9,.$-]*[0-9][0-9,.$-]*")
_CARD_AMOUNT = re.compile(r"-?[0-9]+\.[0-9]{2}")
_GROUPED_CARD_AMOUNT = re.compile(r"-?[0-9]{1,3}(?:,[0-9]{3})+\.[0-9]{2}")


class BoaCsvFormat:
    """Bank of America CSV statements, checking or credit card."""

    key = "boa_csv"
    fitid_trusted = False

    def detect(self, path: Path) -> bool:
        try:
            rows = _csv_rows(_decode(path.read_bytes(), path), path)
        except FormatError:
            return False
        return _find_header(rows) is not None

    def read(self, path: Path, *, statement_account_id: str | None = None) -> Statement:
        account_id = (statement_account_id or "").strip()
        if not account_id:
            raise FormatError(
                f"{path.name}: BoA CSV needs the statement account ID, which the file lacks"
            )

        data = path.read_bytes()
        rows = _csv_rows(_decode(data, path), path)
        found = _find_header(rows)
        if found is None:
            raise FormatError(f"{path.name}: no BoA checking or credit-card header found")
        header_index, layout = found

        summary = _summary(rows[:header_index], path) if layout is _CHECKING else {}
        balances: list[ParsedBalance] = []
        if "beginning" in summary:
            # The beginning balance is the balance before that day's transactions, so it
            # belongs to the day before. Then the check's (prev, next] window includes that day.
            printed, cents = summary["beginning"]
            as_of = printed - timedelta(days=1)
            balances.append(ParsedBalance(as_of=as_of, balance_cents=cents, kind="ledger"))
        if "ending" in summary:
            printed, cents = summary["ending"]
            balances.append(ParsedBalance(as_of=printed, balance_cents=cents, kind="ledger"))
        opening = summary["beginning"][1] if "beginning" in summary else None
        return Statement(
            format_key=self.key,
            sha256=sha256_of(data),
            statement_account_id=account_id,
            account_type_hint=layout.account_type,
            period_start=summary["beginning"][0] if "beginning" in summary else None,
            period_end=summary["ending"][0] if "ending" in summary else None,
            rows=tuple(_transactions(rows, header_index, layout, path, opening)),
            balances=tuple(balances),
        )

    def evidence(self, statement: Statement, row: ParsedRow) -> MatchEvidence:
        """The FITID and whether this institution's FITIDs are trusted. Import recomputes the
        fingerprint from the omsync account, so the one returned here is not used."""
        return evidence_for(statement, row, fitid_trusted=self.fitid_trusted)


def _decode(data: bytes, path: Path) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FormatError(f"{path.name}: not a text file")


def _csv_rows(text: str, path: Path) -> list[list[str]]:
    try:
        return list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:
        raise FormatError(f"{path.name}: not a readable CSV file") from None


def _find_header(rows: list[list[str]]) -> tuple[int, _Layout] | None:
    for index, row in enumerate(rows):
        cells = [cell.strip() for cell in row]
        while cells and not cells[-1]:
            cells.pop()
        layout = _LAYOUTS.get(tuple(cells))
        if layout is not None:
            return index, layout
    return None


def _summary(rows: list[list[str]], path: Path) -> dict[str, tuple[date, int]]:
    """The beginning and ending balances from the summary block above the header."""
    found: dict[str, tuple[date, int]] = {}
    for row in rows:
        match = _SUMMARY.match(row[0].strip()) if row else None
        if match is None:
            continue
        which = match.group(1).lower()
        if which in found:
            raise FormatError(f"{path.name}: more than one {which} balance")
        try:
            as_of = datetime.strptime(match.group(2), "%m/%d/%Y").date()
        except ValueError:
            raise FormatError(f"{path.name}: the {which} balance has an invalid date") from None
        # BoA's summary header has an empty middle column, so the amount is in the third cell.
        amount = next((cell for cell in row[1:] if cell.strip()), "")
        found[which] = (as_of, _money(amount, path, f"the {which} balance"))
    return found


def _transactions(
    rows: list[list[str]],
    header_index: int,
    layout: _Layout,
    path: Path,
    opening: int | None,
) -> list[ParsedRow]:
    parsed: list[ParsedRow] = []
    running = opening  # the balance the next row's Running Bal. must follow, when known
    card = layout.account_type == "credit_card"
    for index in range(header_index + 1, len(rows)):
        row_number = index + 1  # 1-based, for messages only
        raw = _without_trailing_empty(rows[index])
        if not any(cell.strip() for cell in raw):
            continue
        cells = _join_extra_cells(raw, layout)
        if card and len(raw) > len(layout.header):
            _check_rejoined_card_row(raw, cells, layout, path, row_number)
        if layout.amount >= len(cells) or not cells[layout.amount].strip():
            if card:
                # A card table has no balance rows, so an empty Amount means a row was lost.
                raise FormatError(f"{path.name}: row {row_number} has no amount")
            # No amount: a balance line, not a transaction. The in-table "Beginning balance"
            # line starts the running-balance chain when the summary did not give one.
            if running is None and not parsed and layout.running is not None:
                running = _in_table_opening(cells, layout, path, row_number)
            continue
        cells = cells + [""] * (len(layout.header) - len(cells))

        posted_on = _date(cells[layout.date], path, f"row {row_number}")
        if card:
            amount_cents = _card_amount(cells[layout.amount], path, row_number)
        else:
            amount_cents = _money(cells[layout.amount], path, f"row {row_number} amount")
        if layout.running is not None:
            running = _running_balance(cells, layout, running, amount_cents, path, row_number)
        reference = cells[layout.reference].strip() if layout.reference is not None else ""
        parsed.append(
            ParsedRow(
                position=len(parsed) + 1,
                posted_on=posted_on,
                amount_cents=amount_cents,
                payee_raw=cells[layout.description].replace('"', "").strip(),
                memo_raw=None,
                fitid=reference or None,
                raw=dict(zip(layout.header, cells, strict=True)),
            )
        )
    return parsed


def _join_extra_cells(row: list[str], layout: _Layout) -> list[str]:
    """Rejoin a description that a nested quote split into extra cells."""
    extra = len(row) - len(layout.header)
    if extra <= 0:
        return row
    start = layout.description
    joined = ",".join(row[start : start + extra + 1])
    return row[:start] + [joined] + row[start + extra + 1 :]


def _without_trailing_empty(row: list[str]) -> list[str]:
    """Drop empty cells at the end of a row, so a trailing comma is not taken for a split."""
    end = len(row)
    while end and not row[end - 1].strip():
        end -= 1
    return row[:end]


def _check_rejoined_card_row(
    raw: list[str], cells: list[str], layout: _Layout, path: Path, row_number: int
) -> None:
    """Refuse a card row whose extra cells might hide an unquoted thousands separator.

    A card has no running balance to catch a misread, so any doubt is an error. The cell just
    before Amount must not look numeric: "-1,234.00" splits into "-1" and "234.00". The pieces
    joined into the description must not look numeric and must not be empty. A genuine comma,
    as in "ACME, INC", leaves text on both sides and passes.
    """
    before = cells[layout.amount - 1].strip() if 0 < layout.amount < len(cells) else ""
    if _NUMERIC_CELL.fullmatch(before):
        raise FormatError(
            f"{path.name}: ambiguous amount at row {row_number} (unquoted thousands separator)"
        )
    extra = len(raw) - len(layout.header)
    start = layout.description
    for piece in raw[start : start + extra + 1]:
        text = piece.strip()
        if not text or _NUMERIC_CELL.fullmatch(text):
            raise FormatError(
                f"{path.name}: ambiguous description at row {row_number} "
                "(unquoted thousands separator or stray comma)"
            )


def _card_amount(text: str, path: Path, row_number: int) -> int:
    """A card amount: an optional minus, digits, a dot, and exactly two digits.

    Thousands separators are allowed only in the grouped form a quoted amount uses. A $ sign
    or any other shape is refused, so a card amount is never read loosely.
    """
    value = text.strip()
    if _GROUPED_CARD_AMOUNT.fullmatch(value):
        value = value.replace(",", "")
    if not _CARD_AMOUNT.fullmatch(value):
        raise FormatError(f"{path.name}: row {row_number} amount is not a money amount")
    return to_cents(value)


def _in_table_opening(cells: list[str], layout: _Layout, path: Path, row_number: int) -> int | None:
    """The Running Bal. of an in-table "Beginning balance" line, when that is what this is."""
    assert layout.running is not None
    if len(cells) <= layout.running or not cells[layout.running].strip():
        return None
    description = cells[layout.description] if len(cells) > layout.description else ""
    if not _IN_TABLE_OPENING.match(description.strip()):
        return None
    return _money(cells[layout.running], path, f"row {row_number} running balance")


def _running_balance(
    cells: list[str],
    layout: _Layout,
    running: int | None,
    amount_cents: int,
    path: Path,
    row_number: int,
) -> int | None:
    """Check one row's Running Bal. against the balance before it, and return the new balance.

    A blank Running Bal. skips the check and carries the computed balance forward. With no
    balance to start from, the first printed Running Bal. is taken as the starting point.
    """
    assert layout.running is not None
    expected = None if running is None else running + amount_cents
    text = cells[layout.running].strip()
    if not text:
        return expected
    printed = _money(text, path, f"row {row_number} running balance")
    if expected is not None and printed != expected:
        raise FormatError(
            f"{path.name}: running balance does not add up at row {row_number} "
            "(possible unquoted thousands separator or missing row)"
        )
    return printed


def _date(text: str, path: Path, where: str) -> date:
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").date()
    except ValueError:
        raise FormatError(f"{path.name}: {where} has a date that is not MM/DD/YYYY") from None


def _money(text: str, path: Path, where: str) -> int:
    try:
        return to_cents(text.strip().replace("$", ""))
    except ValueError:
        raise FormatError(f"{path.name}: {where} is not an amount") from None
