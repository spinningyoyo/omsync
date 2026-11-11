"""Payee normalization, brand extraction, and merchant similarity scoring."""

import difflib
import re

# Common business suffixes to strip (order matters for regex alternation)
_BUSINESS_SUFFIXES = re.compile(
    r"\b(?:INC|LLC|LTD|CORP|CO|LP|CORPORATION|INCORPORATED|LIMITED)\b\.?",
    re.IGNORECASE,
)

# Store / location number patterns:  #1234, *1234, SQ *, TST *
_STORE_NUMBERS = re.compile(
    r"(?:#\d+|\*\d+)",
    re.IGNORECASE,
)

# POS terminal / square / toast prefixes
_POS_PREFIXES = re.compile(
    r"^(?:SQ\s*\*|TST\s*\*|PAYPAL\s*\*)\s*",
    re.IGNORECASE,
)

# Collapse whitespace
_MULTI_WS = re.compile(r"\s{2,}")


def comparable_payee(text: str) -> str:
    """Reduce a payee string to a loose form for comparing merchants. Used by recurring-detection
    grouping and by merchant-brand matching. The result is for comparison only, not for storage.

    - Lowercase
    - Strip common business suffixes: INC, LLC, LTD, CORP, CO, LP
    - Remove store/location numbers: #1234, *1234
    - Remove POS prefixes: SQ *, TST *, PAYPAL *
    - Collapse multiple whitespace to single space
    - Strip leading/trailing whitespace
    """
    if not text:
        return ""
    result = text.lower()
    result = _POS_PREFIXES.sub("", result)
    result = _STORE_NUMBERS.sub("", result)
    result = _BUSINESS_SUFFIXES.sub("", result)
    result = _MULTI_WS.sub(" ", result)
    return result.strip()


def extract_merchant_brand(payee: str) -> str | None:
    """Extract the core merchant brand from a payee string.

    Handles variations like:
    - SKECHERS.COM vs SKECHERS-USA #93
    - HOMEDEPOT.COM vs THE HOME DEPOT #1074
    - PAYPAL *REI vs REI.COM
    """
    lower = comparable_payee(payee or "")
    if not lower:
        return None

    # PayPal wrapper: extract the merchant name after *
    paypal_m = re.search(r"paypal\s*\*\s*(.+?)(?:\s+\d{3}[-\s]|$)", lower)
    if paypal_m:
        brand = paypal_m.group(1).strip()
        brand = re.sub(r"\s+\d{3}[-\s]\d{3}[-\s]\d{4}.*$", "", brand)
        brand = re.sub(r"\s+[a-z]{2}$", "", brand)
        return brand.strip() if brand else None

    # Strip common suffixes: .com, .COM, store numbers, city/state
    brand = re.sub(r"\.com\b", "", lower)
    brand = re.sub(r"\s*#\s*\d+\b", "", brand)
    brand = re.sub(r"\s+\d{3}[-\s]\d{3}[-\s]\d{4}.*$", "", brand)
    brand = re.sub(r"^(?:the\s+|sq\s+\*\s*)", "", brand)
    brand = re.sub(r"\s+[a-z]{2,}\s+[a-z]{2}$", "", brand)
    brand = re.sub(r"\s+[a-z]{2}$", "", brand)
    return brand.strip() if brand else None


def merchant_brand_similarity(a: str, b: str) -> float:
    """Compare merchant brands, handling .com/store/PayPal variants.

    Returns 1.0 for the same brand, 0.95 when they match without spaces or hyphens, 0.90 when
    one contains the other, and otherwise a difflib ratio. 0.0 when either has no brand.
    """
    brand_a = extract_merchant_brand(a)
    brand_b = extract_merchant_brand(b)
    if not brand_a or not brand_b:
        return 0.0
    if brand_a == brand_b:
        return 1.0
    compact_a = brand_a.replace(" ", "").replace("-", "")
    compact_b = brand_b.replace(" ", "").replace("-", "")
    if compact_a == compact_b:
        return 0.95
    if compact_a in compact_b or compact_b in compact_a:
        return 0.90
    return difflib.SequenceMatcher(None, brand_a, brand_b).ratio()


# Words that describe a PayPal transfer rather than a merchant, such as "PAYPAL INST XFER".
GENERIC_PAYPAL_WORDS = frozenset(
    {"paypal", "inst", "instant", "xfer", "transfer", "payment", "des", "web", "pmts"}
)


def is_generic_paypal_transfer(payee: str) -> bool:
    """True when a PayPal payee's only brand is a generic transfer phrase, or it has no brand.

    Generic processor words are not merchant identity, even when their text matches exactly.
    """
    brand = extract_merchant_brand(payee)
    return brand is None or set(brand.split()) <= GENERIC_PAYPAL_WORDS
