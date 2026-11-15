"""Triage's view of receipt files: which vendor items no keyword list covers yet."""

from collections import Counter

from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.ledger.categorize import load_keywords, match_receipt_item
from omsync.ledger.receipt_vendors import code_categories
from omsync.receipts.vendors import get_vendor


def has_receipt_files(settings: Settings, vendor_key: str) -> bool:
    """True when the vendor's receipts folder holds at least one file the vendor can discover."""
    vendor = get_vendor(vendor_key)
    return bool(vendor.discover(settings.receipts_dir / vendor_key.upper()))


def unknown_receipt_items(
    session: Session, settings: Settings, vendor_key: str, limit: int = 100
) -> list[tuple[str, int]]:
    """Receipt items that no keyword in the vendor's list matches, and whose vendor code has no
    category either, with the receipts' other payments, such as a reward, that no keyword
    matches: (text, count).

    The vendor discovers and parses every file under <data_dir>/receipts/<vendor_key>/. A file
    that cannot be read is skipped, so a bad receipt never blocks triage. Items are grouped by
    their text, ignoring case and extra spaces, and the most frequent comes first.
    """
    vendor = get_vendor(vendor_key)
    keywords = load_keywords(session)
    by_code = code_categories(session, vendor_key)
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for path in vendor.discover(settings.receipts_dir / vendor_key.upper()):
        try:
            receipts = vendor.parse(path)
        except (OSError, ValueError):
            continue
        for receipt in receipts:
            offsets = [
                offset.description
                for charge in vendor.charges(receipt).charges
                for offset in charge.offsets
            ]
            texts = [(item.description, item.code) for item in receipt.items]
            for description, code in [*texts, *((name, None) for name in offsets)]:
                text = " ".join(description.split())
                if not text or match_receipt_item(vendor_key, text, keywords) is not None:
                    continue
                if code is not None and code in by_code:
                    continue
                key = text.casefold()
                display.setdefault(key, text)
                counts[key] += 1
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], display[pair[0]].casefold()))
    return [(display[key], count) for key, count in ranked[:limit]]
