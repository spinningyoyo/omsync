"""Wealthfront Cash Account CSV statements.

Sign rule: Amount is kept as Wealthfront wrote it. Negative means money left the account
(e.g., a transfer out or withdrawal). Deposits and interest payments are positive.
"""

import csv
import io
from datetime import datetime
from pathlib import Path

from omsync.core.money import to_cents
from omsync.ingest.formats.base import (
    FormatError,
    MatchEvidence,
    ParsedRow,
    Statement,
    evidence_for,
    sha256_of,
)

HEADER = ("Transaction date", "Description", "Type", "Amount")


class WealthfrontCsvFormat:
    """Wealthfront Cash Account CSV statements."""

    key = "wealthfront_csv"
    fitid_trusted = False

    def detect(self, path: Path) -> bool:
        try:
            rows = _csv_rows(_decode(path.read_bytes(), path), path)
        except FormatError:
            return False
        return bool(rows and tuple(cell.strip() for cell in rows[0]) == HEADER)

    def read(self, path: Path, *, statement_account_id: str | None = None) -> Statement:
        account_id = (statement_account_id or "").strip()
        if not account_id:
            raise FormatError(
                f"{path.name}: Wealthfront CSV needs the statement account ID, which the file lacks"
            )

        data = path.read_bytes()
        rows = _csv_rows(_decode(data, path), path)
        if not rows or tuple(cell.strip() for cell in rows[0]) != HEADER:
            raise FormatError(f"{path.name}: no Wealthfront cash activity header found")

        parsed_rows: list[ParsedRow] = []
        for index, row in enumerate(rows[1:], start=1):
            if len(row) != len(HEADER):
                raise FormatError(f"{path.name}: row {index} has {len(row)} cells, expected 4")
            date_str, desc, txn_type, amount_str = (cell.strip() for cell in row)
            try:
                posted_on = datetime.strptime(date_str, "%m/%d/%Y").date()
            except ValueError:
                raise FormatError(f"{path.name}: row {index} has an invalid date") from None
            try:
                amount_cents = to_cents(amount_str)
            except (ValueError, TypeError):
                raise FormatError(f"{path.name}: row {index} has an invalid amount") from None

            raw_dict = {
                "Transaction date": date_str,
                "Description": desc,
                "Type": txn_type,
                "Amount": amount_str,
            }
            parsed_rows.append(
                ParsedRow(
                    position=index,
                    posted_on=posted_on,
                    amount_cents=amount_cents,
                    payee_raw=desc,
                    memo_raw=txn_type or None,
                    fitid=None,
                    raw=raw_dict,
                )
            )

        dates = [r.posted_on for r in parsed_rows]
        return Statement(
            format_key=self.key,
            sha256=sha256_of(data),
            statement_account_id=account_id,
            account_type_hint="savings",
            period_start=min(dates) if dates else None,
            period_end=max(dates) if dates else None,
            rows=tuple(parsed_rows),
            balances=(),
        )

    def evidence(self, statement: Statement, row: ParsedRow) -> MatchEvidence:
        return evidence_for(statement, row, fitid_trusted=self.fitid_trusted)


def _decode(data: bytes, path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
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
