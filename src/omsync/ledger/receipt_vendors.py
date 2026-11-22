"""Which receipt vendors omsync reads, which payees their receipts pay for, and the category a
vendor's own item code gives an item that no keyword matches.

The vendors' parsers and connectors are in omsync.receipts. Reconciliation, keyword lists, and
category admin need only what is here, so it lives below all of them. Add a vendor by adding its
VendorTerms to RECEIPT_VENDORS.

An item code's category is a setting, stored as a preference and written directly. Deleting or
merging categories moves it in the same batch (see ledger/categories_admin).
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from omsync.ledger.categories import Category
from omsync.ledger.preferences import get_pref, set_pref

P2P_PAYEES = ("zelle", "venmo", "paypal", "cash app", "cashapp")


@dataclass(frozen=True)
class ItemCode:
    """A vendor's own class for an item, such as Costco's E for food. pref is the preference that
    holds its category. label names the items for a person, such as "Food items"."""

    code: str
    pref: str
    label: str


@dataclass(frozen=True)
class VendorTerms:
    key: str
    is_payee: Callable[[str], bool]
    item_codes: tuple[ItemCode, ...] = ()


def is_amazon_payee(payee: str) -> bool:
    lowered = payee.lower()
    return "amazon" in lowered or "amzn" in lowered


def is_costco_payee(payee: str) -> bool:
    text = str(payee or "")
    if any(keyword in text.lower() for keyword in P2P_PAYEES):
        return False
    normalized = " ".join(text.upper().replace("*", " ").split())
    return "COSTCO" in normalized or "COST CO" in normalized


RECEIPT_VENDORS: tuple[VendorTerms, ...] = (
    VendorTerms("amazon", is_amazon_payee),
    VendorTerms("costco", is_costco_payee, (ItemCode("E", "costco_food_category", "Food items"),)),
)

RECEIPT_VENDOR_KEYS: tuple[str, ...] = tuple(vendor.key for vendor in RECEIPT_VENDORS)

ITEM_CODE_PREFS: tuple[str, ...] = tuple(
    item.pref for vendor in RECEIPT_VENDORS for item in vendor.item_codes
)


def vendor_terms(key: str) -> VendorTerms | None:
    return next((vendor for vendor in RECEIPT_VENDORS if vendor.key == key), None)


def item_code(vendor_key: str, code: str) -> ItemCode | None:
    vendor = vendor_terms(vendor_key)
    if vendor is None:
        return None
    return next((item for item in vendor.item_codes if item.code == code), None)


def is_receipt_merchant(payee: str | None) -> bool:
    """True for a payee whose purchases receipt matching owns."""
    text = payee or ""
    return any(vendor.is_payee(text) for vendor in RECEIPT_VENDORS)


def item_code_category(session: Session, item: ItemCode) -> int | None:
    """The category for the item code, or None when it is not set, or names a category that is
    gone or hidden."""
    stored = get_pref(session, item.pref)
    if not isinstance(stored, int) or isinstance(stored, bool):
        return None
    category = session.get(Category, stored)
    return None if category is None or category.hidden else category.id


def set_item_code_category(session: Session, item: ItemCode, category_id: int | None) -> None:
    """Set the category for the item code, or turn it off with None. Raises ValueError for a
    category that does not exist or is hidden."""
    if category_id is not None:
        category = session.get(Category, category_id)
        if category is None or category.hidden:
            raise ValueError("Pick a category that is not hidden.")
    set_pref(session, item.pref, category_id)


def code_categories(session: Session, vendor_key: str) -> dict[str, int]:
    """The category each of a vendor's item codes gives an item that no keyword matches."""
    vendor = vendor_terms(vendor_key)
    if vendor is None:
        return {}
    found = {item.code: item_code_category(session, item) for item in vendor.item_codes}
    return {code: category for code, category in found.items() if category is not None}
