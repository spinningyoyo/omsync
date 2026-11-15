"""The protocol a receipt vendor implements, and the defaults.

A vendor reads its own files and says which charges its receipts paid. The engine in match.py
and apply.py does the rest, the same way for every vendor.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from omsync.receipts.model import ChargeResult, Receipt

NOT_ENOUGH_DATA = "not enough data to split this receipt across card charges"


@dataclass(frozen=True)
class CandidateTxn:
    """A transaction a vendor may use to infer a receipt's charges.

    payee is the text the vendor recognizes: the raw statement text when it is present.
    account_numbers are the last four digits the account is known by: its own and its aliases'.
    """

    id: int
    posted_on: date
    amount_cents: int
    payee: str
    account_numbers: frozenset[str]


class ReceiptVendor(Protocol):
    key: str  # "amazon", "costco"

    def discover(self, root: Path) -> list[Path]: ...

    def parse(self, path: Path) -> list[Receipt]:
        """Read one file into receipts. Each receipt has a stable ID and a revision."""
        ...

    def is_vendor_payee(self, payee: str) -> bool: ...

    def charges(self, receipt: Receipt) -> ChargeResult:
        """Observed charges, a request for inference, or insufficient data."""
        ...

    def infer_charges(self, receipt: Receipt, candidates: list[CandidateTxn]) -> ChargeResult:
        """Work out charges from candidate transactions, when charges() asked for them."""
        ...

    def receipt_url(self, receipt: Receipt) -> str | None:
        """The vendor's own page for the receipt, or None when it has none."""
        ...


class BaseVendor:
    """Inherit from this to get the default infer_charges and receipt_url.

    The default says the receipt cannot be split and has no web page. A vendor that can infer
    charges overrides infer_charges and still implements the rest of ReceiptVendor itself.
    """

    key: str

    def infer_charges(self, receipt: Receipt, candidates: list[CandidateTxn]) -> ChargeResult:
        return ChargeResult("insufficient", reason=NOT_ENOUGH_DATA)

    def receipt_url(self, receipt: Receipt) -> str | None:
        return None
