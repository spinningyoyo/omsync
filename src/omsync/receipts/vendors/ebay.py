"""eBay order files read into receipts, and the card charges they paid.

Each order is one receipt, built from its items and fees.
An order states its card charges or payment method as recorded in eBay order details.
Each charge is observed, with a window from 3 days before the order date to 30 days after.
When no transaction matches an observed charge, infer_charges looks for transactions
matching the total within the window.

Files: the sync saves one JSON file per order, <provider folder>/<year>/<order ID>.json.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omsync.core import money
from omsync.receipts.model import (
    Charge,
    ChargeResult,
    Receipt,
    ReceiptItem,
    Tender,
    charge_key,
    make_receipt,
)
from omsync.receipts.vendor import BaseVendor, CandidateTxn

logger = logging.getLogger(__name__)

VENDOR_KEY = "ebay"
FOLDER = "EBAY"
ORDER_PAGE = "https://order.ebay.com/ord/show"

OBSERVED_BEFORE_DAYS = 3
OBSERVED_AFTER_DAYS = 30
SPLIT_MAX_CANDIDATES = 24


class EbayFileError(ValueError):
    """An eBay order file cannot be read. The message names the file, never its contents."""


class EbayVendor(BaseVendor):
    """eBay receipt vendor implementation."""

    key = VENDOR_KEY
    folder = FOLDER

    def discover(self, provider_folder: Path) -> list[Path]:
        return sorted(path for path in provider_folder.rglob("*.json") if path.is_file())

    def parse(self, path: Path, provider_key: str) -> list[Receipt]:
        return list(_parse_json_receipt(path, provider_key))

    def receipt_url(self, receipt_id: str) -> str | None:
        return f"{ORDER_PAGE}?orderId={receipt_id}"

    def receipt_label(self, receipt_id: str) -> str:
        return f"eBay order {receipt_id}"

    def charges(self, receipt: Receipt) -> ChargeResult:
        start = receipt.purchased_on - timedelta(days=OBSERVED_BEFORE_DAYS)
        end = receipt.purchased_on + timedelta(days=OBSERVED_AFTER_DAYS)

        cards = [t for t in receipt.tenders if t.kind == "card" and t.amount_cents and t.last4]
        if cards:
            charges = tuple(
                Charge(
                    receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, idx),
                    amount_cents=-t.amount_cents,
                    window_start=start,
                    window_end=end,
                    card_last4=t.last4,
                    item_positions=None,
                    observed=True,
                    anchor=receipt.purchased_on,
                )
                for idx, t in enumerate(cards)
                if t.amount_cents != 0
            )
            return ChargeResult("observed", charges, infer_if_unmatched=True)

        charge = Charge(
            receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, 0),
            amount_cents=-receipt.total_cents,
            window_start=start,
            window_end=end,
            card_last4=None,
            item_positions=None,
            observed=True,
            anchor=receipt.purchased_on,
        )
        return ChargeResult("observed", (charge,), infer_if_unmatched=True)

    def infer_charges(self, receipt: Receipt, candidates: list[CandidateTxn]) -> ChargeResult:
        start = receipt.purchased_on - timedelta(days=OBSERVED_BEFORE_DAYS)
        end = receipt.purchased_on + timedelta(days=OBSERVED_AFTER_DAYS)

        card_last4 = next(
            (t.last4 for t in receipt.tenders if t.kind == "card" and t.last4),
            None,
        )

        usable = [
            cand
            for cand in candidates
            if cand.amount_cents != 0
            and start <= cand.posted_on <= end
            and (card_last4 is None or card_last4 in cand.account_numbers)
        ]
        if len(usable) > SPLIT_MAX_CANDIDATES:
            return ChargeResult(
                "insufficient",
                reason=f"{len(usable)} transactions are in the window, more than checked",
            )

        exact_matches = [cand for cand in usable if abs(cand.amount_cents) == receipt.total_cents]
        if not exact_matches:
            return ChargeResult(
                "insufficient",
                reason="no matching transaction adds up to the order total",
            )
        if len(exact_matches) > 1:
            return ChargeResult(
                "insufficient",
                reason="several possible charge combinations",
            )

        cand = exact_matches[0]
        charge = Charge(
            receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, 1),
            amount_cents=-abs(cand.amount_cents),
            window_start=start,
            window_end=end,
            card_last4=next(iter(cand.account_numbers), None),
            item_positions=None,
            observed=False,
            transaction_id=cand.id,
        )
        return ChargeResult("observed", (charge,))


def _parse_items(raw_items: list[dict[str, Any]], shipping_cents: int) -> list[ReceiptItem]:
    receipt_items: list[ReceiptItem] = []
    for idx, it in enumerate(raw_items):
        title = str(it.get("title") or "Item").strip()
        amt_cents = money.to_cents(it.get("amount_cents", 0))
        qty = int(it.get("quantity", 1))
        receipt_items.append(
            ReceiptItem(
                position=idx,
                description=title,
                amount_cents=amt_cents,
                taxable=True,
                quantity=max(1, qty),
            )
        )

    if shipping_cents > 0:
        receipt_items.append(
            ReceiptItem(
                position=len(receipt_items),
                description="Shipping",
                amount_cents=shipping_cents,
                taxable=False,
                quantity=1,
            )
        )
    return receipt_items


def _parse_tenders(raw_charges: Any, total_cents: int) -> list[Tender]:
    tenders: list[Tender] = []
    if isinstance(raw_charges, list):
        for c in raw_charges:
            if not isinstance(c, dict):
                continue
            amt = money.to_cents(c.get("amount_cents", 0)) if "amount_cents" in c else None
            last4 = str(c.get("card_last4") or "").strip() or None
            method = str(c.get("method") or ("card" if last4 else "other")).lower()
            kind = "card" if last4 else method
            tenders.append(Tender(kind=kind, last4=last4, amount_cents=amt))

    if not tenders:
        tenders.append(Tender(kind="other", last4=None, amount_cents=total_cents))
    return tenders


def _parse_json_receipt(path: Path, provider_key: str) -> list[Receipt]:
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise EbayFileError(f"{path.name} is not a readable JSON file") from None

    if not isinstance(data, dict):
        raise EbayFileError(f"{path.name} is not a readable JSON object")

    order_id = str(data.get("order_id") or "").strip()
    if not order_id:
        raise EbayFileError(f"{path.name} has no order_id")

    order_date_str = str(data.get("order_date") or "").strip()
    try:
        order_day = datetime.fromisoformat(order_date_str).date()
    except ValueError:
        raise EbayFileError(f"{path.name} has an invalid order_date") from None

    total_cents = money.to_cents(data.get("total_cents", 0))
    tax_cents = money.to_cents(data.get("tax_cents", 0))
    shipping_cents = money.to_cents(data.get("shipping_cents", 0))

    receipt_items = _parse_items(data.get("items", []), shipping_cents)
    tenders = _parse_tenders(data.get("charges", []), total_cents)

    receipt = make_receipt(
        vendor_key=VENDOR_KEY,
        receipt_id=order_id,
        purchased_on=order_day,
        account=provider_key,
        items=tuple(receipt_items),
        tax_cents=tax_cents,
        total_cents=total_cents,
        tenders=tuple(tenders),
        is_refund=False,
    )
    return [receipt]
