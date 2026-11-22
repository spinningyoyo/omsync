"""Costco warehouse receipts, read from the JSON files the downloader saves.

A receipt is one JSON file, named <barcode>.json, under receipts/COSTCO/<provider key>/<year>/.
Money is integer cents. A purchase is negative on the card, and a return is positive.

Items keep the descriptions the earlier categorizer used. The engine refuses negative items, so a
return stores its items and tax as positive amounts, and the charge carries the refund's sign.
Lines become items in this order:
- Two lines of one item number whose amounts cancel are a void, and both are dropped.
- A discount or fee line that names an item ("/<item number>") is folded into that item, and so
  takes its category. A receipt item has one taxable flag, so a line folds when its flag matches
  the item's, or when it has no flag, as Costco prints its discounts.
- A line that names an item on none of this receipt's lines is a price adjustment of an earlier
  purchase. It takes the description and code of that item on the latest earlier receipt, so the
  item's keywords, or its code, categorize it.
- A credit left negative, such as a coupon taken back on a return, is shared across the positive
  items in proportion to their amounts. A receipt whose credits exceed its items goes to review.
- A discount or fee line of zero, or with no amount, is dropped.
The item identifier (E for food, F, or other) is carried as the item's code. Keywords categorize an
item first; a food item that no keyword matches takes the category set for Costco food (see
ledger/receipt_vendors.py). A folded line keeps its item's code.

Charges: each card tender with an amount is one observed charge. When a receipt has several card
tenders, each one is a charge that covers all items (item_positions None), so allocation scales
each card's share across the same categories. Non-card tenders (shop card, cash, rebates) make no
charge. When one card paid the rest of them, they are offsets of its charge: the items keep their
full price, and each tender is a line named by its description, such as "2% REBATE", which the
keyword list categorizes. A card tender with no amount sends the receipt to review. A card number
with no readable last four digits, or only zeros, as on a return, names no card: its charge has an
unknown payer. Each charge may post from the purchase date up to five days later. A receipt that
totals zero, such as an even exchange, moved no money and yields no receipt.

Payees: Costco or COST CO, except P2P payees (Zelle, Venmo, PayPal, Cash App).
"""

import functools
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from omsync.core import money
from omsync.ledger.receipt_vendors import is_costco_payee
from omsync.receipts.model import (
    Charge,
    ChargeResult,
    Offset,
    Receipt,
    ReceiptItem,
    Tender,
    charge_key,
    make_receipt,
)
from omsync.receipts.vendor import BaseVendor

VENDOR_KEY = "costco"
VENDOR_LOGIN = "costco"
FOLDER = "COSTCO"  # under the data directory's receipts folder
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
    flagged: bool  # the line has a tax flag at all
    quantity: int
    item_number: str
    links_to: str | None
    adjustment: bool  # a discount, fee, or coupon line rather than a product
    code: str | None  # Costco's itemIdentifier, such as E for food


class CostcoVendor(BaseVendor):
    key: str = VENDOR_KEY
    folder: str = FOLDER

    def discover(self, provider_folder: Path) -> list[Path]:
        return sorted(
            path
            for path in provider_folder.rglob("*.json")
            if path.is_file()
            and not path.name.startswith(".")
            and not path.parent.name.startswith(".")
        )

    def parse(self, path: Path, provider_key: str) -> list[Receipt]:
        data = load_receipt_file(path)
        if _cents(data.get("total"), "total") == 0:
            return []
        # <provider folder>/<year>/<barcode>.json: the price adjustments look at the same provider.
        earlier = _earlier_items(path.parent.parent, receipt_date(data, path.name))
        return [parse_receipt(data, path.name, earlier, account=provider_key)]

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
        if len(charges) == 1:
            charges = [replace(charges[0], offsets=_offsets(receipt))]
        return ChargeResult("observed", tuple(charges))


def _offsets(receipt: Receipt) -> tuple[Offset, ...]:
    """The receipt's non-card payments, as offsets of its one card charge. None unless every
    payment has an amount and the payments add up to the total, so the items' full price is
    known."""
    others = [t for t in receipt.tenders if t.kind != "card" and t.amount_cents != 0]
    if not others or any(t.amount_cents is None for t in receipt.tenders):
        return ()
    if sum(t.amount_cents or 0 for t in receipt.tenders) != receipt.total_cents:
        return ()
    return tuple(Offset(t.kind, t.amount_cents or 0) for t in others)


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


def parse_receipt(
    data: dict[str, Any],
    name: str = "receipt",
    earlier: Mapping[str, tuple[str, str | None]] | None = None,
    *,
    account: str,
) -> Receipt:
    """Build the engine's Receipt from one Costco receipt object. earlier maps an item number to
    its description and code on an earlier receipt, for price adjustments. Raises
    CostcoParseError."""
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
        account=account,
        items=_items(data.get("itemArray"), sign, earlier or {}),
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


def item_description(item: dict[str, Any]) -> str:
    """The item's two description fields, joined with single spaces."""
    parts = [_text(item.get(key)) for key in ("itemDescription01", "itemDescription02")]
    return " ".join(" ".join(part for part in parts if part).split())


def extract_slash_item_number(item: dict[str, Any]) -> str | None:
    """The item number a discount or fee line names, from "/<number>", or None. "/0" names no
    item, and Costco writes it in one field while the other names the item."""
    for key in ("frenchItemDescription1", "itemDescription01"):
        match = _SLASH_LINK.match(_text(item.get(key)))
        if match and match.group(1).strip("0"):
            return match.group(1)
    return None


def is_taxable(tax_flag: Any) -> bool:
    return _text(tax_flag).upper() in TAXABLE_FLAGS


def last_four(display_number: Any) -> str | None:
    """The last four digits of a card number as the receipt displays it, or None. Zeros stand in
    for a number the receipt does not show."""
    digits = _LAST_DIGITS.sub("", _text(display_number))
    if len(digits) < 4 or not digits.strip("0"):
        return None
    return digits[-4:]


def _items(
    raw_items: Any, sign: int, earlier: Mapping[str, tuple[str, str | None]]
) -> tuple[ReceiptItem, ...]:
    lines = _without_voids(_lines(raw_items, sign))
    first_by_number: dict[str, _Line] = {}
    for line in lines:
        if not line.adjustment and line.item_number:
            first_by_number.setdefault(line.item_number, line)

    kept: list[_Line] = []
    for line in lines:
        target = first_by_number.get(line.links_to) if line.links_to is not None else None
        if target is not None and (target.taxable == line.taxable or not line.flagged):
            target.amount_cents += line.amount_cents
            continue
        if target is None and line.links_to in earlier:
            line.description, line.code = earlier[line.links_to]
        if line.adjustment and line.amount_cents == 0:
            continue
        kept.append(line)
    _share_credits(kept)
    return tuple(
        ReceiptItem(
            position=line.position,
            description=line.description,
            amount_cents=line.amount_cents,
            taxable=line.taxable,
            quantity=line.quantity,
            code=line.code,
        )
        for line in kept
    )


def _without_voids(lines: list[_Line]) -> list[_Line]:
    """The lines without each pair of product lines of one item number that cancel."""
    voided: set[int] = set()
    open_lines: dict[tuple[str, int], list[_Line]] = {}
    for line in lines:
        if line.adjustment or not line.item_number:
            continue
        partners = open_lines.get((line.item_number, -line.amount_cents))
        if partners:
            voided.update({partners.pop().position, line.position})
        else:
            open_lines.setdefault((line.item_number, line.amount_cents), []).append(line)
    return [line for line in lines if line.position not in voided]


def _share_credits(lines: list[_Line]) -> None:
    """Share each negative line across the positive lines by amount, in place. The largest
    remainders take the leftover cents, so the shares add up to the credit exactly. Nothing changes
    when the credits are larger than the positive lines."""
    credit_lines = [line for line in lines if line.amount_cents < 0]
    items = [line for line in lines if line.amount_cents > 0]
    total = sum(line.amount_cents for line in items)
    credit = -sum(line.amount_cents for line in credit_lines)
    if not credit_lines or not items or credit > total:
        return
    shares = [line.amount_cents * credit // total for line in items]
    by_remainder = sorted(
        range(len(items)),
        key=lambda i: (-(items[i].amount_cents * credit % total), items[i].position),
    )
    for i in by_remainder[: credit - sum(shares)]:
        shares[i] += 1
    for line, share in zip(items, shares, strict=True):
        line.amount_cents -= share
    for line in credit_lines:
        lines.remove(line)


def _lines(raw_items: Any, sign: int) -> list[_Line]:
    rows = _list(raw_items, "itemArray")
    lines: list[_Line] = []
    for position, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            raise CostcoParseError(f"item {position} is not an object")
        adjustment = _is_adjustment(raw)
        amount = raw.get("amount")
        cents = 0 if amount is None and adjustment else _cents(amount, f"item {position} amount")
        lines.append(
            _Line(
                position=position,
                description=item_description(raw),
                amount_cents=sign * cents,
                taxable=is_taxable(raw.get("taxFlag")),
                flagged=bool(_text(raw.get("taxFlag"))),
                quantity=_quantity(raw.get("unit")),
                item_number=_text(raw.get("itemNumber")),
                links_to=extract_slash_item_number(raw),
                adjustment=adjustment,
                code=_text(raw.get("itemIdentifier")) or None,
            )
        )
    return lines


def _is_adjustment(raw: dict[str, Any]) -> bool:
    """Whether a line is a discount, fee, or coupon: its description starts with "/", or with
    "CPN/" for a coupon."""
    texts = [_text(raw.get(key)) for key in ("frenchItemDescription1", "itemDescription01")]
    return any(text.startswith(("/", "CPN/")) for text in texts)


def _earlier_items(root: Path, before: date) -> dict[str, tuple[str, str | None]]:
    """Each item number's description and code on the latest receipt under root dated before
    `before`."""
    latest: dict[str, tuple[date, str, str | None]] = {}
    for day, number, description, code in _product_lines(root, _folder_state(root)):
        if day < before and (number not in latest or latest[number][0] < day):
            latest[number] = (day, description, code)
    return {number: (description, code) for number, (_day, description, code) in latest.items()}


def _folder_state(root: Path) -> tuple[tuple[str, int], ...]:
    """The receipt files under root and their modification times, so a change reads them again."""
    if not root.exists():
        return ()
    return tuple(
        (str(path), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*.json"))
        if path.is_file() and not path.name.startswith(".") and not path.parent.name.startswith(".")
    )


@functools.lru_cache(maxsize=4)
def _product_lines(
    root: Path, state: tuple[tuple[str, int], ...]
) -> tuple[tuple[date, str, str, str | None], ...]:
    """(purchase date, item number, description, code) for every product line of every readable
    receipt in state. A file that cannot be read is left out here, and parse reports it."""
    found: list[tuple[date, str, str, str | None]] = []
    for path_text, _mtime in state:
        path = Path(path_text)
        try:
            data = load_receipt_file(path)
            day = receipt_date(data, path.name)
            rows = _list(data.get("itemArray"), "itemArray")
        except CostcoParseError:
            continue
        for raw in rows:
            if isinstance(raw, dict) and not _is_adjustment(raw):
                number = _text(raw.get("itemNumber"))
                if number:
                    code = _text(raw.get("itemIdentifier")) or None
                    found.append((day, number, item_description(raw), code))
    return tuple(found)


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
