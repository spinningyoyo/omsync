"""The receipt vendors omsync reads, and the registry entries for them.

Add a vendor by importing its class here and adding an instance to BUILT_IN_VENDORS.
ensure_registered puts each built-in vendor into the registry once, on first use.
"""

from omsync.receipts.vendor import ReceiptVendor, get_vendor, register
from omsync.receipts.vendors.amazon import AmazonVendor
from omsync.receipts.vendors.costco import CostcoVendor

BUILT_IN_VENDORS: tuple[ReceiptVendor, ...] = (AmazonVendor(), CostcoVendor())


def ensure_registered() -> None:
    """Register each built-in vendor whose key is not in the registry yet."""
    for vendor in BUILT_IN_VENDORS:
        try:
            get_vendor(vendor.key)
        except KeyError:
            register(vendor)


__all__ = ["BUILT_IN_VENDORS", "ensure_registered"]
