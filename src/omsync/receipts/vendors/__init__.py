"""The receipt vendors omsync reads.

Add a vendor by importing its class here and adding an instance to BUILT_IN_VENDORS. Its key, payee
wording, and item codes are in omsync.ledger.receipt_vendors, and its connector registers in
omsync.receipts.connectors.
"""

from omsync.ledger.receipt_vendors import RECEIPT_VENDOR_KEYS
from omsync.receipts.vendor import ReceiptVendor
from omsync.receipts.vendors.amazon import AmazonVendor
from omsync.receipts.vendors.costco import CostcoVendor
from omsync.receipts.vendors.ebay import EbayVendor
from omsync.receipts.vendors.walmart import WalmartVendor

BUILT_IN_VENDORS: tuple[ReceiptVendor, ...] = (
    AmazonVendor(),
    CostcoVendor(),
    EbayVendor(),
    WalmartVendor(),
)

if {vendor.key for vendor in BUILT_IN_VENDORS} != set(RECEIPT_VENDOR_KEYS):
    raise RuntimeError("BUILT_IN_VENDORS and RECEIPT_VENDORS list different vendors")


def get_vendor(key: str) -> ReceiptVendor:
    """Return the vendor with this key. Raises KeyError if there is none."""
    for vendor in BUILT_IN_VENDORS:
        if vendor.key == key:
            return vendor
    raise KeyError(f"no receipt vendor named {key!r}")


__all__ = ["BUILT_IN_VENDORS", "get_vendor"]
