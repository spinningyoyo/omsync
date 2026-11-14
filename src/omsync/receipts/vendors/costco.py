"""Costco warehouse receipts, read from the JSON files the downloader saves.

A receipt is one JSON file, named <barcode>.json, under receipts/COSTCO/<year>/<quarter>/.
Money is integer cents. A purchase is negative on the card, and a return is positive.

Items keep the descriptions the earlier categorizer used. A discount or fee line that names an
item ("/<item number>") is folded into that item, and so takes its category. A fold happens only
when both lines carry the same taxable flag, because a receipt item has one flag. The engine
refuses negative items, so:
- a return stores its items and tax as positive amounts, and the charge carries the refund's sign;
- a credit line that names no item stays negative, and its receipt goes to review.
The item identifier (E, F, or other) is not carried. The engine categorizes items from their
keyword lists, not from that code.

Charges: each card tender with an amount is one observed charge. When a receipt has several card
tenders, each one is a charge that covers all items (item_positions None), so allocation scales
each card's share across the same categories. Non-card tenders (shop card, cash, rebates) make no
charge. A card tender with no amount, or with no readable last four digits, sends the receipt to
review. Each charge may post from the purchase date up to five days later.

Payees: Costco or COST CO, except P2P payees (Zelle, Venmo, PayPal, Cash App).
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from omsync.core import money
from omsync.ledger.receipt_vendors import is_costco_payee
from omsync.receipts.model import (
    Charge,
    ChargeResult,
    Receipt,
    ReceiptItem,
    Tender,
    charge_key,
    make_receipt,
)
from omsync.receipts.vendor import BaseVendor

VENDOR_KEY = "costco"
VENDOR_LOGIN = "costco"
RECEIPTS_FOLDER = "COSTCO"  # under the data directory's receipts folder
CHARGE_WINDOW_DAYS = 5  # the earlier matcher looked up to five days after the purchase

TAXABLE_FLAGS = frozenset({"A", "B", "C", "D", "Y", "T"})
NON_CARD_TENDERS = (
    "SHOP CARD",
    "CASH",
    "2% REBATE",
    "CITI REBATE",
    "CITI REWARD",
    "REWARD CERTIFICATE",
    "EXECUTIVE REBATE",
)
TENDER_AMOUNT_KEYS = ("amountTender", "amount", "tenderAmount", "paymentAmount")
_SLASH_LINK = re.compile(r"^/\s*(\d+)")
_LAST_DIGITS = re.compile(r"\D")


class CostcoParseError(ValueError):
    """A receipt file could not be read. The message names a file or field, never its contents."""


@dataclass
class _Line:
    """One itemArray line. Its amount is signed in the receipt's direction, positive for items."""

    position: int
    description: str
    amount_cents: int
    taxable: bool
    quantity: int
    item_number: str
    links_to: str | None


class CostcoVendor(BaseVendor):
    key: str = VENDOR_KEY

    def discover(self, root: Path) -> list[Path]:
        return sorted(path for path in root.glob("*/*/*.json") if path.is_file())

    def parse(self, path: Path) -> list[Receipt]:
        data = load_receipt_file(path)
        return [parse_receipt(data, path.name)]

    def is_vendor_payee(self, payee: str) -> bool:
        return is_costco_payee(payee)

    def charges(self, receipt: Receipt) -> ChargeResult:
        if not receipt.items:
            return _insufficient("the receipt lists no items")
        if any(item.amount_cents < 0 for item in receipt.items):
            return _insufficient("a discount or credit line names no item on the receipt")
        cards = [
            (index, tender) for index, tender in enumerate(receipt.tenders) if tender.kind == "card"
        ]
        if not cards:
            return _insufficient("paid without a card")

        window_end = receipt.purchased_on + timedelta(days=CHARGE_WINDOW_DAYS)
        charges: list[Charge] = []
        for index, tender in cards:
            if tender.amount_cents is None:
                return _insufficient("a card tender has no amount")
            if tender.last4 is None:
                return _insufficient("a card tender has no readable last four digits")
            if tender.amount_cents == 0:
                continue
            paid = abs(tender.amount_cents)
            charges.append(
                Charge(
                    receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, index),
                    amount_cents=paid if receipt.is_refund else -paid,
                    window_start=receipt.purchased_on,
                    window_end=window_end,
                    card_last4=tender.last4,
                    item_positions=None,
                    observed=True,
                )
            )
        if not charges:
            return _insufficient("the card tenders add up to nothing")
        return ChargeResult("observed", tuple(charges))


def load_receipt_file(path: Path) -> dict[str, Any]:
    """Read one receipt file as a JSON object, with numbers as Decimal.

    Malformed JSON raises CostcoParseError with no part of the file in the message. The decode
    error is raised outside its except block, so it is not chained to the message.
    """
    text: str | None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    data: object = None
    if text is not None:
        try:
            data = json.loads(text, parse_float=Decimal, parse_int=Decimal)
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        raise CostcoParseError(f"{path.name} is not a readable receipt object")
    return data


def parse_receipt(data: dict[str, Any], name: str = "receipt") -> Receipt:
    """Build the engine's Receipt from one Costco receipt object. Raises CostcoParseError."""
    barcode = _text(data.get("transactionBarcode"))
    if not barcode:
        raise CostcoParseError(f"{name} has no transactionBarcode")
    purchased_on = receipt_date(data, name)
    total = _cents(data.get("total"), "total")
    is_refund = total < 0
    sign = -1 if is_refund else 1
    tax = _cents(data.get("taxes") or 0, "taxes")
    if is_refund:
        tax = abs(tax)  # a return's tax is positive here; the charge's sign carries the refund
    return make_receipt(
        vendor_key=VENDOR_KEY,
        receipt_id=barcode,
        purchased_on=purchased_on,
        vendor_login=VENDOR_LOGIN,
        items=_items(data.get("itemArray"), sign),
        tax_cents=tax,
        total_cents=total,
        tenders=_tenders(data.get("tenderArray")),
        is_refund=is_refund,
    )


def receipt_date(data: dict[str, Any], name: str = "receipt") -> date:
    """The local date of the purchase: transactionDate, else the date part of transactionDateTime.

    The local date is kept as written. It is never converted to UTC.
    """
    day = _text(data.get("transactionDate"))
    if day:
        return _timestamp(day, "%Y-%m-%d", f"{name} transactionDate").date()
    moment = _text(data.get("transactionDateTime"))
    if moment:
        return _timestamp(moment, "%Y-%m-%dT%H:%M:%S", f"{name} transactionDateTime").date()
    raise CostcoParseError(f"{name} has no transactionDate")


def quarter_folder(day: date) -> str:
    """Q1 to Q4, for the folder a receipt is saved in."""
    return f"Q{(day.month - 1) // 3 + 1}"


def item_description(item: dict[str, Any]) -> str:
    """The item's two description fields, joined with single spaces."""
    parts = [_text(item.get(key)) for key in ("itemDescription01", "itemDescription02")]
    return " ".join(" ".join(part for part in parts if part).split())


def extract_slash_item_number(item: dict[str, Any]) -> str | None:
    """The item number a discount or fee line names, from "/<number>", or None."""
    for key in ("frenchItemDescription1", "itemDescription01"):
        match = _SLASH_LINK.match(_text(item.get(key)))
        if match:
            return match.group(1)
    return None


def is_taxable(tax_flag: Any) -> bool:
    return _text(tax_flag).upper() in TAXABLE_FLAGS


def last_four(display_number: Any) -> str | None:
    """The last four digits of a card number as the receipt displays it, or None."""
    digits = _LAST_DIGITS.sub("", _text(display_number))
    return digits[-4:] if len(digits) >= 4 else None


def _items(raw_items: Any, sign: int) -> tuple[ReceiptItem, ...]:
    lines = _lines(raw_items, sign)
    first_by_number: dict[str, _Line] = {}
    for line in lines:
        if line.links_to is None and line.item_number:
            first_by_number.setdefault(line.item_number, line)

    kept: list[_Line] = []
    for line in lines:
        target = first_by_number.get(line.links_to) if line.links_to is not None else None
        if target is not None and target.taxable == line.taxable:
            target.amount_cents += line.amount_cents
        else:
            kept.append(line)
    return tuple(
        ReceiptItem(
            position=line.position,
            description=line.description,
            amount_cents=line.amount_cents,
            taxable=line.taxable,
            quantity=line.quantity,
        )
        for line in kept
    )


def _lines(raw_items: Any, sign: int) -> list[_Line]:
    rows = _list(raw_items, "itemArray")
    lines: list[_Line] = []
    for position, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            raise CostcoParseError(f"item {position} is not an object")
        lines.append(
            _Line(
                position=position,
                description=item_description(raw),
                amount_cents=sign * _cents(raw.get("amount"), f"item {position} amount"),
                taxable=is_taxable(raw.get("taxFlag")),
                quantity=_quantity(raw.get("unit")),
                item_number=_text(raw.get("itemNumber")),
                links_to=extract_slash_item_number(raw),
            )
        )
    return lines


def _tenders(raw_tenders: Any) -> tuple[Tender, ...]:
    tenders: list[Tender] = []
    for raw in _list(raw_tenders, "tenderArray"):
        if not isinstance(raw, dict):
            raise CostcoParseError("a tender is not an object")
        description = _text(raw.get("tenderDescription")).upper()
        kind = description if _is_non_card(description) else "card"
        tenders.append(
            Tender(
                kind=kind,
                last4=last_four(raw.get("displayAccountNumber")),
                amount_cents=_tender_amount(raw),
            )
        )
    return tuple(tenders)


def _tender_amount(raw: dict[str, Any]) -> int | None:
    for key in TENDER_AMOUNT_KEYS:
        value = raw.get(key)
        if value is not None and value != "":
            return _cents(value, "tender amount")
    return None


def _is_non_card(description: str) -> bool:
    return any(keyword in description for keyword in NON_CARD_TENDERS)


def _quantity(value: Any) -> int:
    """A whole-number unit count of at least one, or 1 when the unit is anything else."""
    if isinstance(value, Decimal) and value == value.to_integral_value() and value >= 1:
        return int(value)
    return 1


def _list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CostcoParseError(f"{field} is not a list")
    return value


def _cents(value: Any, field: str) -> int:
    try:
        return money.to_cents(value)
    except (TypeError, ValueError):
        raise CostcoParseError(f"{field} is not an amount") from None


def _timestamp(text: str, pattern: str, field: str) -> datetime:
    try:
        return datetime.strptime(text, pattern)
    except ValueError:
        raise CostcoParseError(f"{field} is not in the expected format") from None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(int(value))
    return str(value).strip()


def _insufficient(reason: str) -> ChargeResult:
    return ChargeResult("insufficient", reason=reason)
