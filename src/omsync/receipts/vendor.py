"""The protocol a receipt vendor implements, the registry that finds vendors, and the defaults.

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
    account_last4 is None when the account has no last four digits.
    """

    id: int
    posted_on: date
    amount_cents: int
    payee: str
    account_last4: str | None


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


class BaseVendor:
    """Inherit from this to get the default infer_charges.

    The default says the receipt cannot be split. A vendor that can infer charges overrides
    infer_charges and still implements the rest of ReceiptVendor itself.
    """

    key: str

    def infer_charges(self, receipt: Receipt, candidates: list[CandidateTxn]) -> ChargeResult:
        return ChargeResult("insufficient", reason=NOT_ENOUGH_DATA)


_VENDORS: dict[str, ReceiptVendor] = {}


def register(vendor: ReceiptVendor) -> None:
    """Make a vendor findable by its key. A key may be registered only once."""
    if vendor.key in _VENDORS:
        raise ValueError(f"receipt vendor {vendor.key!r} is already registered")
    _VENDORS[vendor.key] = vendor


def vendor_keys() -> list[str]:
    """The keys of every registered vendor, sorted."""
    return sorted(_VENDORS)


def get_vendor(key: str) -> ReceiptVendor:
    """Return the vendor registered under key. Raises KeyError if there is none."""
    try:
        return _VENDORS[key]
    except KeyError:
        raise KeyError(f"no receipt vendor named {key!r}") from None
