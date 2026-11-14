"""Which receipt vendors omsync reads, and which payees their receipts pay for.

The vendors themselves are in omsync.receipts.vendors. Reconciliation and keyword lists need only
the keys and the payee wording, so they live here, below both.
"""

RECEIPT_VENDOR_KEYS: tuple[str, ...] = ("amazon", "costco")

P2P_PAYEES = ("zelle", "venmo", "paypal", "cash app", "cashapp")


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
