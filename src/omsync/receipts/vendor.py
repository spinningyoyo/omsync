"""The protocol a receipt vendor implements, and the defaults.

A vendor reads its own files and says which charges its receipts paid. The engine in match.py
and apply.py does the rest, the same way for every vendor.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from omsync.ledger.receipt_vendors import vendor_terms
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
    folder: str  # the vendor's folder under the receipts directory, such as "COSTCO"

    def provider_folder(self, receipts_dir: Path, provider_key: str) -> Path:
        """Where one provider's receipt files are kept."""
        ...

    def receipt_file(
        self, receipts_dir: Path, provider_key: str, day: date, receipt_id: str
    ) -> Path:
        """Where the file of one receipt is kept: <provider folder>/<year>/<file name>.json.
        Raises ValueError when the receipt ID cannot be a file name."""
        ...

    def discover(self, provider_folder: Path) -> list[Path]: ...

    def parse(self, path: Path, provider_key: str) -> list[Receipt]:
        """Read one file of this provider into receipts. Each receipt has a stable ID and a
        revision."""
        ...

    def is_vendor_payee(self, payee: str) -> bool: ...

    def charges(self, receipt: Receipt) -> ChargeResult:
        """Observed charges, a request for inference, or insufficient data."""
        ...

    def infer_charges(self, receipt: Receipt, candidates: list[CandidateTxn]) -> ChargeResult:
        """Work out charges from candidate transactions, when charges() asked for them."""
        ...

    def receipt_url(self, receipt_id: str) -> str | None:
        """The vendor's own page for the receipt, or None when it has none."""
        ...

    def receipt_label(self, receipt_id: str) -> str:
        """The receipt's name for a person, such as "Costco receipt 2104…"."""
        ...


class BaseVendor:
    """Inherit from this to get the default is_vendor_payee, infer_charges, receipt_url, and
    receipt_label. is_vendor_payee reads the vendor's payee wording in
    omsync.ledger.receipt_vendors.

    The default says the receipt cannot be split and has no web page. A vendor that can infer
    charges overrides infer_charges and still implements the rest of ReceiptVendor itself.
    """

    key: str
    folder: str

    def provider_folder(self, receipts_dir: Path, provider_key: str) -> Path:
        return receipts_dir / self.folder / provider_key

    def receipt_file(
        self, receipts_dir: Path, provider_key: str, day: date, receipt_id: str
    ) -> Path:
        if (
            not receipt_id
            or receipt_id != Path(receipt_id).name
            or receipt_id in (".", "..")
            or "\\" in receipt_id
            or "\0" in receipt_id
        ):
            raise ValueError("the receipt ID cannot be a file name")
        folder = self.provider_folder(receipts_dir, provider_key)
        return folder / str(day.year) / f"{receipt_id}.json"

    def is_vendor_payee(self, payee: str) -> bool:
        terms = vendor_terms(self.key)
        return terms is not None and terms.is_payee(payee)

    def infer_charges(self, receipt: Receipt, candidates: list[CandidateTxn]) -> ChargeResult:
        return ChargeResult("insufficient", reason=NOT_ENOUGH_DATA)

    def receipt_url(self, receipt_id: str) -> str | None:
        return None

    def receipt_label(self, receipt_id: str) -> str:
        return f"{self.key.capitalize()} receipt {receipt_id}"
