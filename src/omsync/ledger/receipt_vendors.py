"""Which receipt vendors omsync reads, which payees their receipts pay for, and the category a
vendor's own item code gives an item that no keyword matches.

The vendors themselves are in omsync.receipts.vendors. Reconciliation, keyword lists, and category
admin need only the keys, the payee wording, and the code categories, so they live here, below all
of them.

Costco marks each food item with the code E. The category for Costco food is a setting, stored as a
preference and written directly. Deleting or merging categories moves it in the same batch (see
ledger/categories_admin).
"""

from sqlalchemy.orm import Session

from omsync.ledger.categories import Category
from omsync.ledger.preferences import get_pref, set_pref

RECEIPT_VENDOR_KEYS: tuple[str, ...] = ("amazon", "costco")

P2P_PAYEES = ("zelle", "venmo", "paypal", "cash app", "cashapp")

COSTCO_FOOD_CODE = "E"
COSTCO_FOOD_CATEGORY = "costco_food_category"


def is_amazon_payee(payee: str) -> bool:
    lowered = payee.lower()
    return "amazon" in lowered or "amzn" in lowered


def is_costco_payee(payee: str) -> bool:
    text = str(payee or "")
    if any(keyword in text.lower() for keyword in P2P_PAYEES):
        return False
    normalized = " ".join(text.upper().replace("*", " ").split())
    return "COSTCO" in normalized or "COST CO" in normalized


def is_receipt_merchant(payee: str | None) -> bool:
    """True for a payee whose purchases receipt matching owns."""
    text = payee or ""
    return is_amazon_payee(text) or is_costco_payee(text)


def costco_food_category(session: Session) -> int | None:
    """The category for Costco food items that no keyword matches, or None when it is not set, or
    names a category that is gone or hidden."""
    stored = get_pref(session, COSTCO_FOOD_CATEGORY)
    if not isinstance(stored, int) or isinstance(stored, bool):
        return None
    category = session.get(Category, stored)
    return None if category is None or category.hidden else category.id


def set_costco_food_category(session: Session, category_id: int | None) -> None:
    """Set the category for Costco food items that no keyword matches, or turn it off with None.
    Raises ValueError for a category that does not exist or is hidden."""
    if category_id is not None:
        category = session.get(Category, category_id)
        if category is None or category.hidden:
            raise ValueError("Pick a category that is not hidden.")
    set_pref(session, COSTCO_FOOD_CATEGORY, category_id)


def code_categories(session: Session, vendor_key: str) -> dict[str, int]:
    """The category each of a vendor's item codes gives an item that no keyword matches."""
    if vendor_key == "costco":
        food = costco_food_category(session)
        if food is not None:
            return {COSTCO_FOOD_CODE: food}
    return {}
