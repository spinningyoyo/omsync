"""Amazon: the order files a sync downloads, read into receipts, and the card charges they paid.

Each order is one receipt, built from its items. Each refund in it is a refund receipt. Its items
are found in this order:
- the order's items that add up to the refund exactly, when some do;
- else its units that add up to the refund exactly, so one of two units returned is found;
- else, when the order has one product, that product, as for a return fee or a price adjustment;
- else one item carries the refund and goes to triage.

An item's amount already includes its tax and shipping, so items are not marked taxable. The
engine then spreads no tax, and each group is scaled to the charge from its exact amounts. The
order's tax is kept on the receipt as a fact.

Charges:
- An order states its card charges, as Amazon's payment history lists them. Each is observed,
  with a window from 3 days before the earlier of its date and the order date, to 30 days after
  the later of them. When no transaction has a stated charge, the order is tried as split charges:
  infer_charges looks for transactions that add up to the total, because Amazon sometimes charges
  each item, or the bank shows one charge as several. It accepts only one answer: no answer or
  several answers go to review. Its window runs from 1 day before the order to 21 days after the
  order or the latest charge date, whichever is later. An order that names no card gives inferred
  charges with no card, which are unknown payer and never applied.
- A charge that names no card was paid by gift card or points: with one card charge, it is an
  offset of that charge, and with no card charge, no charge is expected.
- A refund is observed: one positive charge, dated from 1 day before to 10 days after the refund.
  When no transaction has its amount, the refund is tried as split credits, one per item group,
  with the same exact-combination rule. Amazon credits each item separately, sometimes.

Files: the sync saves one JSON file per order, <provider folder>/<year>/<order ID>.json. A file
that cannot be read raises AmazonFileError (a ValueError) with the file name. The message never
includes the file's contents.
"""

import json
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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
from omsync.receipts.vendor import BaseVendor, CandidateTxn

VENDOR_KEY = "amazon"
FOLDER = "AMAZON"  # under the data directory's receipts folder
ORDER_PAGE = "https://www.amazon.com/gp/your-account/order-details"
REFUND_ID_PART = "-refund-"

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
GIFT_OFFSET = "Gift card or points"
GIFT_ONLY_REASON = "this order was paid only with a gift card, so no card charge is expected"
NO_COMBINATION_REASON = "no exact combination of transactions adds up to the order total"
REFUND_NO_COMBINATION_REASON = "no exact combination of credits adds up to the refund"
SEVERAL_COMBINATIONS_REASON = "several possible charge combinations"
UNTIED_CREDITS_REASON = "the credits cannot be tied to the refunded items one by one"


class AmazonFileError(ValueError):
    """An Amazon order file cannot be read. The message names the file, never its contents."""


@dataclass(frozen=True)
class AmazonReceipt(Receipt):
    """A receipt with each item's quantity in the same order as its items.

    quantities is not part of the revision, which covers the facts the engine knows about. An
    empty quantities means one of each.
    """

    quantities: tuple[int, ...] = ()


class AmazonVendor(BaseVendor):
    key = VENDOR_KEY
    folder = FOLDER

    def discover(self, provider_folder: Path) -> list[Path]:
        return sorted(path for path in provider_folder.rglob("*.json") if path.is_file())

    def parse(self, path: Path, provider_key: str) -> list[Receipt]:
        return list(_parse_json_receipt(path, provider_key))

    def receipt_url(self, receipt_id: str) -> str | None:
        return f"{ORDER_PAGE}?{urlencode({'orderID': _order_id(receipt_id)})}"

    def receipt_label(self, receipt_id: str) -> str:
        order_id = _order_id(receipt_id)
        kind = "refund of order" if order_id != receipt_id else "order"
        return f"Amazon {kind} {order_id}"

    def charges(self, receipt: Receipt) -> ChargeResult:
        if receipt.is_refund:
            return _refund_charges(receipt)
        gifts = [tender for tender in receipt.tenders if tender.kind == "gift"]
        if gifts:
            if _card(receipt) is None:
                return ChargeResult("insufficient", reason=GIFT_ONLY_REASON)
            amounts = [tender.amount_cents for tender in receipt.tenders]
            if None in amounts or sum(a or 0 for a in amounts) != receipt.total_cents:
                return ChargeResult("insufficient", reason=GIFT_REASON)
        start, end = _observed_window(receipt)
        card_tenders = [
            t for t in receipt.tenders if t.kind == "card" and t.amount_cents is not None
        ]
        if gifts:
            return _gift_and_card_charges(receipt, card_tenders, gifts, start, end)
        if len(card_tenders) > 1:
            charges = tuple(
                Charge(
                    receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, idx),
                    amount_cents=-t.amount_cents,
                    window_start=start,
                    window_end=end,
                    card_last4=t.last4,
                    item_positions=None,
                    observed=True,
                    anchor=min(receipt.ship_dates, default=None),
                )
                for idx, t in enumerate(card_tenders)
                if t.amount_cents != 0
            )
            return ChargeResult("observed", charges, infer_if_unmatched=True)
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
        return ChargeResult("observed", (charge,), infer_if_unmatched=True)

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
                and (card is None or card in candidate.account_numbers)
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
        # An order is inferred only after its stated charges found nothing. Its keys start at 1, so
        # they never collide with the first stated charge's key 0.
        first_key = 1
        # A charge that names no card keeps card_last4 None: its payer is unknown, so it is
        # reviewed, not applied, even though its transaction is known.
        charges = tuple(
            Charge(
                receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, first_key + index),
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


def _gift_and_card_charges(
    receipt: Receipt, cards: list[Tender], gifts: list[Tender], start: date, end: date
) -> ChargeResult:
    """The card charges of an order that a gift card or points paid in part, every amount known.
    One card charge carries the gift payments as offsets, so the items keep their full price. The
    total includes the gift payments, so no transactions are inferred from it."""
    paid = [t for t in cards if t.amount_cents]
    offsets = tuple(Offset(GIFT_OFFSET, t.amount_cents or 0) for t in gifts if t.amount_cents)
    charges = tuple(
        Charge(
            receipt_key=charge_key(VENDOR_KEY, receipt.receipt_id, idx),
            amount_cents=-(t.amount_cents or 0),
            window_start=start,
            window_end=end,
            card_last4=t.last4,
            item_positions=None,
            observed=True,
            anchor=min(receipt.ship_dates, default=None),
            offsets=offsets if len(paid) == 1 else (),
        )
        for idx, t in enumerate(paid)
    )
    return ChargeResult("observed", charges)


# Reading files


def _parse_date(value: str) -> date | None:
    cleaned = value.strip()
    try:
        return datetime.fromisoformat(cleaned).date()
    except ValueError:
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _json_items_and_quantities(
    raw_items: list[dict[str, Any]], total_cents: int
) -> tuple[list[ReceiptItem], list[int]]:
    items: list[ReceiptItem] = []
    quantities: list[int] = []
    for idx, it in enumerate(raw_items, start=1):
        desc = str(it.get("description") or it.get("title") or "Amazon Item").strip()
        amt = int(
            it.get("amount_cents")
            if it.get("amount_cents") is not None
            else (it.get("unit_price_cents", 0) * it.get("quantity", 1))
        )
        qty = int(it.get("quantity", 1))
        items.append(ReceiptItem(position=idx, description=desc, amount_cents=amt, taxable=False))
        quantities.append(qty)

    if not items and total_cents > 0:
        items.append(
            ReceiptItem(
                position=1, description="Amazon Order", amount_cents=total_cents, taxable=False
            )
        )
        quantities.append(1)
    return items, quantities


def _json_tenders(charges_data: list[dict[str, Any]], total_cents: int) -> tuple[Tender, ...]:
    tenders: list[Tender] = []
    for c in charges_data:
        last4 = c.get("card_last4") or None
        c_amt = int(c.get("amount_cents", total_cents))
        # Amazon lists gift card balance and points as charges with no card.
        tenders.append(Tender(kind="card" if last4 else "gift", last4=last4, amount_cents=c_amt))
    if not tenders:
        tenders.append(Tender(kind="card", last4=None, amount_cents=total_cents))
    return tuple(tenders)


def _json_refund_receipts(
    data: dict[str, Any],
    order_receipt: AmazonReceipt,
    order_id: str,
    account: str,
) -> list[AmazonReceipt]:
    receipts: list[AmazonReceipt] = []
    for r in data.get("refunds", []):
        r_amt = int(r.get("amount_cents", 0))
        if r_amt <= 0:
            continue
        r_date = _parse_date(r.get("date", "")) or order_receipt.purchased_on
        r_last4 = r.get("card_last4") or _card(order_receipt)
        r_reason = str(r.get("reason") or "Customer Return").strip()
        r_receipt_id = f"{order_id}{REFUND_ID_PART}{r_date.isoformat()}-{r_amt}"

        r_items = _refund_items(order_receipt, r_amt) or (
            ReceiptItem(
                position=1,
                description=f"Refund: {r_reason}",
                amount_cents=r_amt,
                taxable=False,
            ),
        )

        refund_receipt = make_receipt(
            vendor_key=VENDOR_KEY,
            receipt_id=r_receipt_id,
            purchased_on=r_date,
            account=account,
            items=r_items,
            tax_cents=0,
            total_cents=r_amt,
            tenders=() if r_last4 is None else (Tender("card", r_last4, r_amt),),
            is_refund=True,
            refund_of=order_id,
        )
        receipts.append(_as_amazon(refund_receipt))
    return receipts


def _parse_json_receipt(path: Path, account: str) -> list[AmazonReceipt]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AmazonFileError(f"{path.name} is not a readable JSON file") from exc

    order_id = str(data.get("order_id") or path.stem).strip()
    if not order_id:
        raise AmazonFileError(f"{path.name} has no order_id")

    charges_data = data.get("charges", [])
    raw_date = data.get("order_date")
    order_date = _parse_date(raw_date) if raw_date else None
    if order_date is None and charges_data:
        order_date = _parse_date(charges_data[0].get("date", ""))
    order_date = order_date or date.today()

    tax_cents = int(data.get("tax_cents") or 0)
    total_cents = int(data.get("total_cents") or 0)
    if total_cents <= 0 and charges_data:
        total_cents = sum(int(c.get("amount_cents", 0)) for c in charges_data)

    items, quantities = _json_items_and_quantities(data.get("items", []), total_cents)
    if total_cents <= 0:
        total_cents = sum(it.amount_cents for it in items)

    tenders = _json_tenders(charges_data, total_cents)
    # Amazon charges the card as each shipment leaves, so a charge's date is its ship date.
    charge_days = sorted(
        {day for c in charges_data if (day := _parse_date(str(c.get("date") or ""))) is not None}
    )
    ship_dates = tuple(charge_days) or (order_date,)

    base = make_receipt(
        vendor_key=VENDOR_KEY,
        receipt_id=order_id,
        purchased_on=order_date,
        account=account,
        items=items,
        tax_cents=tax_cents,
        total_cents=total_cents,
        tenders=tenders,
        is_refund=False,
        ship_dates=ship_dates,
    )
    # The charges are stated exactly, so matching uses them rather than inferring.
    order_receipt = _as_amazon(base, quantities=tuple(quantities))
    return [order_receipt, *_json_refund_receipts(data, order_receipt, order_id, account)]


def _as_amazon(base: Receipt, *, quantities: tuple[int, ...] = ()) -> AmazonReceipt:
    values = {field.name: getattr(base, field.name) for field in fields(Receipt)}
    return AmazonReceipt(**values, quantities=quantities)


def _order_id(receipt_id: str) -> str:
    """The order a receipt ID names: a refund's ID starts with its order's."""
    return receipt_id.split(REFUND_ID_PART, 1)[0]


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


def _refund_items(order: Receipt, refund_cents: int) -> tuple[ReceiptItem, ...] | None:
    """What a refund paid back, or None: whole items that add up to it, else units that add up to
    it, else the order's one product. Each later step runs only when the earlier ones find
    nothing, so a refund the whole items explain never changes."""
    return (
        _whole_items(order, refund_cents)
        or _units(order, refund_cents)
        or _one_product(order, refund_cents)
    )


def _whole_items(order: Receipt, refund_cents: int) -> tuple[ReceiptItem, ...] | None:
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


def _units(order: Receipt, refund_cents: int) -> tuple[ReceiptItem, ...] | None:
    """The units that add up to the refund exactly, as when one of two units was returned, or None.
    A line of several units splits into equal units only when its amount divides evenly. Searches
    up to 8 units."""
    quantities = order.quantities if isinstance(order, AmazonReceipt) else ()
    units: list[int] = []
    owners: list[int] = []
    for index, item in enumerate(order.items):
        quantity = quantities[index] if index < len(quantities) else 1
        if quantity > 1 and item.amount_cents % quantity == 0:
            units += [item.amount_cents // quantity] * quantity
            owners += [index] * quantity
        else:
            units.append(item.amount_cents)
            owners.append(index)
    if len(units) == len(order.items) or len(units) > SPLIT_MAX_ITEMS:
        return None
    sums = _subset_sums(units)
    for mask in range(1, 1 << len(units)):
        if sums[mask] == refund_cents:
            refunded: dict[int, int] = {}
            for bit in _bits(mask, len(units)):
                refunded[owners[bit]] = refunded.get(owners[bit], 0) + units[bit]
            return tuple(
                ReceiptItem(
                    position=number,
                    description=order.items[index].description,
                    amount_cents=amount,
                    taxable=False,
                )
                for number, (index, amount) in enumerate(sorted(refunded.items()), start=1)
            )
    return None


def _one_product(order: Receipt, refund_cents: int) -> tuple[ReceiptItem, ...] | None:
    """The order's one product, for a refund that is not a whole number of its units, such as a
    return fee kept back or a price adjustment, or None. A refund larger than the product is not
    explained by it."""
    if len(order.items) != 1 or refund_cents > order.items[0].amount_cents:
        return None
    return (ReceiptItem(1, order.items[0].description, refund_cents, taxable=False),)


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
            and card in candidate.account_numbers
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
