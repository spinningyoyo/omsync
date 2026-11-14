"""The receipt vendors omsync reads.

Add a vendor by importing its class here, adding an instance to BUILT_IN_VENDORS, and adding its
key to RECEIPT_VENDOR_KEYS in omsync.ledger.receipt_vendors.
"""

from omsync.ledger.receipt_vendors import RECEIPT_VENDOR_KEYS
from omsync.receipts.vendor import ReceiptVendor
from omsync.receipts.vendors.amazon import AmazonVendor
from omsync.receipts.vendors.costco import CostcoVendor

BUILT_IN_VENDORS: tuple[ReceiptVendor, ...] = (AmazonVendor(), CostcoVendor())

if tuple(vendor.key for vendor in BUILT_IN_VENDORS) != RECEIPT_VENDOR_KEYS:
    raise RuntimeError("BUILT_IN_VENDORS and RECEIPT_VENDOR_KEYS list different vendors")


def get_vendor(key: str) -> ReceiptVendor:
    """Return the vendor with this key. Raises KeyError if there is none."""
    for vendor in BUILT_IN_VENDORS:
        if vendor.key == key:
            return vendor
    raise KeyError(f"no receipt vendor named {key!r}")


__all__ = ["BUILT_IN_VENDORS", "get_vendor"]
