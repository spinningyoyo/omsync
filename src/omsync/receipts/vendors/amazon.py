"""Amazon: order history and refund exports, read into receipts, and the card charges they paid.

Each order is one receipt, built from its item rows. Each completed refund row is a refund
receipt. Its items are the order's items that add up to the refund exactly, when some do;
otherwise one item carries the refund and goes to triage.

Amazon's Total Amount already includes each item's tax and shipping, so items are not marked
taxable. The engine then spreads no tax, and each group is scaled to the charge from its exact
Total Amount sums. The order's tax is kept on the receipt as a fact.

Charges:
- A single-shipment order paid with a known card is observed: one charge for the total. Its window
  runs from 3 days before the earlier of the ship date and the order date, to 30 days after the
  later of them. Without a ship date, the order date is used.
- An order with several shipments may be charged once per shipment, so it needs transactions.
  infer_charges looks for transactions that add up to the total. It accepts only one answer:
  no answer or several answers go to review. Its window runs from 1 day before the order to
  21 days after the order or the latest ship date, whichever is later. An order that names no
  card gives inferred charges with no card, which are unknown payer and never applied.
- A gift card plus a card is insufficient. Amazon does not say how much the card paid.
- A refund is observed: one positive charge, dated from 1 day before to 10 days after the refund.
  When no transaction has its amount, the refund is tried as split credits, one per item group,
  with the same exact-combination rule. Amazon credits each item separately, sometimes.

Files: <root>/<vendor login>/Your Amazon Orders/Order History.csv and
<root>/<vendor login>/Your Returns & Refunds/Refund Details.csv. The vendor login is the folder
name. Refunds take their card and items from the same login's order history.

Rows that cannot be read raise AmazonFileError (a ValueError) with the file name and row number.
The message never includes the row's contents.
"""

import csv
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path

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

VENDOR_KEY = "amazon"
ORDER_FILE = "Order History.csv"
ORDERS_FOLDER = "Your Amazon Orders"
REFUND_FILE = "Refund Details.csv"

# Observed single-shipment charges: from 3 days before the order to 30 days after.
OBSERVED_BEFORE_DAYS = 3
OBSERVED_AFTER_DAYS = 30
# Split-shipment candidates: from 1 day before the order to 21 days after.
SPLIT_BEFORE_DAYS = 1
SPLIT_AFTER_DAYS = 21
# Refund charges: from 1 day before the refund to 10 days after.
REFUND_BEFORE_DAYS = 1
REFUND_AFTER_DAYS = 10
# Combinatorial guards: items per order to search, and transactions per window to check.
SPLIT_MAX_ITEMS = 8
SPLIT_MAX_CANDIDATES = 24

GIFT_REASON = "Amazon does not say how much was paid by gift card"
GIFT_ONLY_REASON = "this order was paid only with a gift card, so no card charge is expected"
NO_COMBINATION_REASON = "no exact combination of transactions adds up to the order total"
REFUND_NO_COMBINATION_REASON = "no exact combination of credits adds up to the refund"
SEVERAL_COMBINATIONS_REASON = "several possible charge combinations"
UNTIED_CREDITS_REASON = "the credits cannot be tied to the refunded items one by one"

_ORDER_COLUMNS = (
    "Order ID",
    "Order Date",
    "Order Status",
    "Ship Date",
    "Product Name",
    "Total Amount",
    "Shipment Item Subtotal Tax",
    "Payment Method Type",
)
_REFUND_COLUMNS = ("Order ID", "Refund Amount", "Refund Date", "Payment Status", "Reversal Reason")
_MISSING = frozenset({"", "not available", "na", "nan", "none"})


class AmazonFileError(ValueError):
    """An Amazon export cannot be read. The message names the file and row, never its contents."""


@dataclass(frozen=True)
class AmazonReceipt(Receipt):
    """A receipt with the number of shipments in its order.

    shipments is not part of the revision, which covers the facts the engine knows about.
    """

    shipments: int = 1


class AmazonVendor(BaseVendor):
    key = VENDOR_KEY

    def discover(self, root: Path) -> list[Path]:
        return sorted(root.rglob(ORDER_FILE)) + sorted(root.rglob(REFUND_FILE))

    def parse(self, path: Path) -> list[Receipt]:
        login = path.parent.parent.name
        if path.name == ORDER_FILE:
            return list(_order_receipts(path, login))
        if path.name == REFUND_FILE:
            return list(_refund_receipts(path, login))
        raise AmazonFileError(f"{path.name} is not an Amazon order or refund file")

    def is_vendor_payee(self, payee: str) -> bool:
        lowered = payee.lower()
        return "amazon" in lowered or "amzn" in lowered

    def charges(self, receipt: Receipt) -> ChargeResult:
        if receipt.is_refund:
            return _refund_charges(receipt)
        if any(tender.kind == "gift" for tender in receipt.tenders):
            if _card(receipt) is None:
                return ChargeResult("insufficient", reason=GIFT_ONLY_REASON)
            return ChargeResult("insufficient", reason=GIFT_REASON)
        if _shipments(receipt) > 1:
            return ChargeResult("needs_transactions")
        start, end = _observed_window(receipt)
        charge = Charge(
            receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, 0),
            amount_cents=-receipt.total_cents,
            window_start=start,
            window_end=end,
            card_last4=_card(receipt),
            item_positions=None,
            observed=True,
            anchor=min(receipt.ship_dates, default=None),
        )
        return ChargeResult("observed", (charge,))

    def infer_charges(self, receipt: Receipt, candidates: list[CandidateTxn]) -> ChargeResult:
        if receipt.is_refund:
            return _infer_refund_charges(receipt, candidates)
        item_count = len(receipt.items)
        if not 2 <= item_count <= SPLIT_MAX_ITEMS:
            return ChargeResult(
                "insufficient",
                reason=f"split matching needs 2 to {SPLIT_MAX_ITEMS} items, "
                f"and this order has {item_count}",
            )
        card = _card(receipt)
        start, end = _split_window(receipt)
        usable = sorted(
            (
                candidate
                for candidate in candidates
                if candidate.amount_cents != 0
                and start <= candidate.posted_on <= end
                and (card is None or candidate.account_last4 == card)
            ),
            key=lambda candidate: (candidate.posted_on, candidate.id),
        )
        if len(usable) > SPLIT_MAX_CANDIDATES:
            return ChargeResult(
                "insufficient",
                reason=f"{len(usable)} transactions are in the split window, more than the "
                f"{SPLIT_MAX_CANDIDATES} that are checked",
            )
        combinations = _exact_combinations(
            [abs(candidate.amount_cents) for candidate in usable],
            receipt.total_cents,
            limit=item_count,
        )
        if not combinations:
            return ChargeResult("insufficient", reason=NO_COMBINATION_REASON)
        if len(combinations) > 1:
            return ChargeResult("insufficient", reason=SEVERAL_COMBINATIONS_REASON)

        chosen = [usable[index] for index in combinations[0]]
        amounts = [abs(candidate.amount_cents) for candidate in chosen]
        positions = _item_positions(receipt.items, amounts) if len(chosen) > 1 else None
        # A charge that names no card keeps card_last4 None: its payer is unknown, so it is
        # reviewed, not applied, even though its transaction is known.
        charges = tuple(
            Charge(
                receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, index),
                amount_cents=-amount,
                window_start=start,
                window_end=end,
                card_last4=card,
                item_positions=None if positions is None else positions[index],
                observed=False,
                transaction_id=candidate.id,
            )
            for index, (candidate, amount) in enumerate(zip(chosen, amounts, strict=True))
        )
        return ChargeResult("observed", charges)


# Reading files


def _read_rows(path: Path, columns: Sequence[str]) -> list[tuple[int, dict[str, str]]]:
    """Every data row with its row number (the header is row 1), stripped of surrounding spaces."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            for column in columns:
                if column not in header:
                    raise AmazonFileError(f"{path.name} has no {column!r} column")
            rows = [
                (
                    reader.line_num,
                    {key.strip(): (value or "").strip() for key, value in row.items() if key},
                )
                for row in reader
            ]
    except (UnicodeDecodeError, csv.Error):
        raise AmazonFileError(f"{path.name} is not a readable CSV file") from None
    return rows


def _row_error(path: Path, line: int, message: str) -> AmazonFileError:
    return AmazonFileError(f"{path.name} row {line}: {message}")


def _money(value: str, path: Path, line: int, column: str) -> int:
    try:
        return money.to_cents(value)
    except ValueError:
        raise _row_error(path, line, f"{column} is not an amount") from None


def _date(value: str, path: Path, line: int, column: str) -> date:
    parsed = _parse_date(value)
    if parsed is None:
        raise _row_error(path, line, f"{column} is not a date")
    return parsed


def _parse_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _blank(value: str) -> bool:
    return value.strip().lower() in _MISSING


def _card_last4(payment_method: str) -> str | None:
    """The last four digits from text such as "Visa - 1111", or None."""
    last = payment_method.split(" - ")[-1].strip()
    return last if len(last) == 4 and last.isdigit() else None


def _tenders(payment_method: str, total_cents: int) -> tuple[Tender, ...]:
    if _blank(payment_method):
        return ()
    last4 = _card_last4(payment_method)
    if "gift" in payment_method.lower():
        gift = (Tender("gift", None, None),)
        return gift if last4 is None else (*gift, Tender("card", last4, None))
    return (Tender("card", last4, total_cents),)


# Orders


def _order_receipts(path: Path, login: str) -> list[AmazonReceipt]:
    grouped: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for line, row in _read_rows(path, _ORDER_COLUMNS):
        order_id = row["Order ID"]
        if not order_id:
            raise _row_error(path, line, "Order ID is blank")
        _date(row["Order Date"], path, line, "Order Date")
        _money(row["Total Amount"], path, line, "Total Amount")
        if not _blank(row["Ship Date"]):
            _date(row["Ship Date"], path, line, "Ship Date")
        if not _blank(row["Shipment Item Subtotal Tax"]):
            _money(row["Shipment Item Subtotal Tax"], path, line, "Shipment Item Subtotal Tax")
        grouped.setdefault(order_id, []).append((line, row))

    receipts: list[AmazonReceipt] = []
    for order_id, group in grouped.items():
        receipt = _build_order(path, login, order_id, group)
        if receipt is not None:
            receipts.append(receipt)
    return receipts


def _build_order(
    path: Path, login: str, order_id: str, group: list[tuple[int, dict[str, str]]]
) -> AmazonReceipt | None:
    """One order from its rows. Cancelled orders and orders that total nothing are skipped."""
    _, first = group[0]
    if first["Order Status"].lower() == "cancelled":
        return None
    total = sum(_money(row["Total Amount"], path, line, "Total Amount") for line, row in group)
    if total <= 0:
        return None

    items: list[ReceiptItem] = []
    for line, row in group:
        amount = _money(row["Total Amount"], path, line, "Total Amount")
        if amount == 0:
            continue  # Free items carry no money, and would otherwise need a category.
        description = row["Product Name"] or "Unnamed item"
        items.append(
            ReceiptItem(
                position=len(items) + 1,
                description=description,
                amount_cents=amount,
                taxable=False,
            )
        )
    ship_dates = {
        _date(row["Ship Date"], path, line, "Ship Date")
        for line, row in group
        if not _blank(row["Ship Date"])
    }
    tax_text = first["Shipment Item Subtotal Tax"]
    tax = 0
    if not _blank(tax_text):
        tax = _money(tax_text, path, group[0][0], "Shipment Item Subtotal Tax")
    base = make_receipt(
        vendor_key=VENDOR_KEY,
        receipt_id=order_id,
        purchased_on=_date(first["Order Date"], path, group[0][0], "Order Date"),
        vendor_login=login,
        items=items,
        tax_cents=tax,
        total_cents=total,
        tenders=_tenders(first["Payment Method Type"], total),
        ship_dates=ship_dates,
    )
    return _as_amazon(base, shipments=max(len(ship_dates), 1))


def _as_amazon(base: Receipt, *, shipments: int = 1) -> AmazonReceipt:
    values = {field.name: getattr(base, field.name) for field in fields(Receipt)}
    return AmazonReceipt(**values, shipments=shipments)


def _shipments(receipt: Receipt) -> int:
    return receipt.shipments if isinstance(receipt, AmazonReceipt) else 1


def _card(receipt: Receipt) -> str | None:
    for tender in receipt.tenders:
        if tender.kind == "card" and tender.last4:
            return tender.last4
    return None


def _window(receipt: Receipt, before: int, after: int) -> tuple[date, date]:
    return (
        receipt.purchased_on - timedelta(days=before),
        receipt.purchased_on + timedelta(days=after),
    )


def _observed_window(receipt: Receipt) -> tuple[date, date]:
    """A single shipment's window, anchored on its ship date when Amazon gives one.

    The window starts at the earlier of the ship and order dates, and ends at the later, so
    a ship date only ever widens the order-date window.
    """
    anchor = min(receipt.ship_dates, default=receipt.purchased_on)
    earliest = min(anchor, receipt.purchased_on)
    latest = max(anchor, receipt.purchased_on)
    return (
        earliest - timedelta(days=OBSERVED_BEFORE_DAYS),
        latest + timedelta(days=OBSERVED_AFTER_DAYS),
    )


def _split_window(receipt: Receipt) -> tuple[date, date]:
    """Split-shipment candidates. The end covers the latest ship date, if that is later."""
    latest = max(receipt.ship_dates, default=receipt.purchased_on)
    return (
        receipt.purchased_on - timedelta(days=SPLIT_BEFORE_DAYS),
        max(receipt.purchased_on, latest) + timedelta(days=SPLIT_AFTER_DAYS),
    )


# Refunds


def _refund_receipts(path: Path, login: str) -> list[AmazonReceipt]:
    orders = _orders_by_id(path, login)
    seen: set[tuple[str, int, date]] = set()
    receipts: list[AmazonReceipt] = []
    for line, row in _read_rows(path, _REFUND_COLUMNS):
        if row["Payment Status"].lower() != "completed":
            continue
        order_id = row["Order ID"]
        if not order_id:
            raise _row_error(path, line, "Order ID is blank")
        refund_date = _date(row["Refund Date"], path, line, "Refund Date")
        refund_cents = _money(row["Refund Amount"], path, line, "Refund Amount")
        if refund_cents <= 0:
            continue
        key = (order_id, refund_cents, refund_date)
        if key in seen:
            continue
        seen.add(key)
        receipts.append(
            _refund_receipt(
                order_id,
                refund_date,
                refund_cents,
                row["Reversal Reason"],
                login,
                orders.get(order_id),
            )
        )
    return receipts


def _orders_by_id(refund_path: Path, login: str) -> dict[str, Receipt]:
    """The same login's order receipts, keyed by order ID, when the order history is present."""
    order_path = refund_path.parent.parent / ORDERS_FOLDER / ORDER_FILE
    if not order_path.is_file():
        return {}
    return {receipt.receipt_id: receipt for receipt in _order_receipts(order_path, login)}


def _refund_receipt(
    order_id: str,
    refund_date: date,
    refund_cents: int,
    reason: str,
    login: str,
    order: Receipt | None,
) -> AmazonReceipt:
    items = _refund_items(order, refund_cents) if order is not None else None
    if items is None:
        description = f"Refund: {reason}" if reason else "Refund"
        items = (ReceiptItem(1, description, refund_cents, taxable=False),)
    card = _card(order) if order is not None else None
    base = make_receipt(
        vendor_key=VENDOR_KEY,
        receipt_id=f"{order_id}-refund-{refund_date.isoformat()}-{refund_cents}",
        purchased_on=refund_date,
        vendor_login=login,
        items=items,
        tax_cents=0,
        total_cents=refund_cents,
        tenders=() if card is None else (Tender("card", card, refund_cents),),
        is_refund=True,
        refund_of=order_id,
    )
    return _as_amazon(base)


def _refund_items(order: Receipt, refund_cents: int) -> tuple[ReceiptItem, ...] | None:
    """The order's items that add up to the refund exactly, or None. Searches up to 8 items."""
    items = order.items
    if not items or len(items) > SPLIT_MAX_ITEMS:
        return None
    sums = _subset_sums([item.amount_cents for item in items])
    for mask in range(1, 1 << len(items)):
        if sums[mask] == refund_cents:
            chosen = [items[bit] for bit in _bits(mask, len(items))]
            return tuple(
                ReceiptItem(
                    position=number,
                    description=item.description,
                    amount_cents=item.amount_cents,
                    taxable=False,
                )
                for number, item in enumerate(chosen, start=1)
            )
    return None


def _refund_charges(receipt: Receipt) -> ChargeResult:
    """One credit for the whole refund. If none posted, Amazon may have credited each item."""
    start, end = _window(receipt, REFUND_BEFORE_DAYS, REFUND_AFTER_DAYS)
    charge = Charge(
        receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, 0),
        amount_cents=receipt.total_cents,
        window_start=start,
        window_end=end,
        card_last4=_card(receipt),
        item_positions=None,
        observed=True,
    )
    return ChargeResult("observed", (charge,), infer_if_unmatched=True)


def _infer_refund_charges(receipt: Receipt, candidates: list[CandidateTxn]) -> ChargeResult:
    """Credits that add up to a refund, one per group of refunded items.

    Keys are numbered from 1, so they never collide with the whole-refund charge's key 0. The
    refund names a card, so only credits on that card count. Exactly one set of credits must add
    up to the refund, and each credit must be tied to a distinct group of items.
    """
    card = _card(receipt)
    if card is None:
        return ChargeResult("insufficient", reason="the refund names no card")
    start, end = _window(receipt, REFUND_BEFORE_DAYS, REFUND_AFTER_DAYS)
    usable = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.amount_cents > 0
            and start <= candidate.posted_on <= end
            and candidate.account_last4 == card
        ),
        key=lambda candidate: (candidate.posted_on, candidate.id),
    )
    if len(usable) > SPLIT_MAX_CANDIDATES:
        return ChargeResult(
            "insufficient",
            reason=f"{len(usable)} credits are in the refund window, more than the "
            f"{SPLIT_MAX_CANDIDATES} that are checked",
        )
    combinations = _exact_combinations(
        [candidate.amount_cents for candidate in usable],
        receipt.total_cents,
        limit=len(receipt.items),
    )
    if not combinations:
        return ChargeResult("insufficient", reason=REFUND_NO_COMBINATION_REASON)
    if len(combinations) > 1:
        return ChargeResult("insufficient", reason=SEVERAL_COMBINATIONS_REASON)

    chosen = [usable[index] for index in combinations[0]]
    amounts = [candidate.amount_cents for candidate in chosen]
    positions: list[tuple[int, ...]] | None = None
    if len(chosen) > 1:
        positions = _item_positions(receipt.items, amounts)
        if positions is None:
            return ChargeResult("insufficient", reason=UNTIED_CREDITS_REASON)
    charges = tuple(
        Charge(
            receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, index + 1),
            amount_cents=amount,
            window_start=start,
            window_end=end,
            card_last4=card,
            item_positions=None if positions is None else positions[index],
            observed=False,
            transaction_id=candidate.id,
        )
        for index, (candidate, amount) in enumerate(zip(chosen, amounts, strict=True))
    )
    return ChargeResult("observed", charges)


# Split shipments


def _exact_combinations(amounts: list[int], target: int, *, limit: int) -> list[tuple[int, ...]]:
    """Index sets whose amounts add up to target, with at most `limit` indexes each.

    Amounts are positive. The search stops once two sets are found, because two is already
    too many to choose between.
    """
    order = sorted(range(len(amounts)), key=lambda index: amounts[index])
    found: list[tuple[int, ...]] = []
    chosen: list[int] = []

    def walk(start: int, remaining: int) -> None:
        if len(found) > 1:
            return
        if remaining == 0:
            found.append(tuple(sorted(chosen)))
            return
        if len(chosen) == limit:
            return
        for offset in range(start, len(order)):
            index = order[offset]
            if amounts[index] > remaining:
                break
            chosen.append(index)
            walk(offset + 1, remaining - amounts[index])
            chosen.pop()
            if len(found) > 1:
                return

    walk(0, target)
    return found


def _item_positions(
    items: tuple[ReceiptItem, ...], amounts: list[int]
) -> list[tuple[int, ...]] | None:
    """Give each charge the items that add up to its amount, if exactly one way does.

    Returns None when no assignment or several assignments exist. The caller then lets each
    charge cover every item.
    """
    sums = _subset_sums([item.amount_cents for item in items])
    full = (1 << len(items)) - 1
    options = [[mask for mask in range(1, full + 1) if sums[mask] == amount] for amount in amounts]
    solutions: list[list[int]] = []
    picked: list[int] = []

    def walk(charge: int, used: int) -> None:
        if len(solutions) > 1:
            return
        if charge == len(amounts):
            if used == full:
                solutions.append(list(picked))
            return
        for mask in options[charge]:
            if mask & used:
                continue
            picked.append(mask)
            walk(charge + 1, used | mask)
            picked.pop()

    walk(0, 0)
    if len(solutions) != 1:
        return None
    return [tuple(items[bit].position for bit in _bits(mask, len(items))) for mask in solutions[0]]


def _subset_sums(amounts: list[int]) -> list[int]:
    """sums[mask] is the total of the amounts whose bits are set in mask."""
    sums = [0] * (1 << len(amounts))
    for mask in range(1, len(sums)):
        low = mask & -mask
        sums[mask] = sums[mask ^ low] + amounts[low.bit_length() - 1]
    return sums


def _bits(mask: int, count: int) -> list[int]:
    return [bit for bit in range(count) if mask >> bit & 1]
