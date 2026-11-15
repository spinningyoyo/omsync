"""Receipts as a vendor reports them, and the charges that paid for them.

Money is integer cents. A charge is signed like a transaction: negative for a purchase,
positive for a refund.

A receipt's revision is a SHA-256 digest of its facts. A receipt match records the revision
it was applied at, so a receipt that changes later goes to review instead of being applied
again. Vendors build receipts with make_receipt, which computes the revision.

Ship dates are matching evidence, not facts: they shape the windows a charge is searched in, but
they are not hashed, so a receipt that gains or moves a ship date stays already applied.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

ChargeKind = Literal["observed", "needs_transactions", "insufficient"]


@dataclass(frozen=True)
class ReceiptItem:
    """One line on a receipt. amount_cents is net of the item's own discounts. code is the vendor's
    own class for the item, such as Costco's E for food: evidence for its category when no keyword
    matches, and not part of the revision."""

    position: int
    description: str
    amount_cents: int
    taxable: bool
    quantity: int = 1
    code: str | None = None


@dataclass(frozen=True)
class Tender:
    """How part of a receipt was paid. amount_cents None means the amount is unknown."""

    kind: str
    last4: str | None
    amount_cents: int | None


@dataclass(frozen=True)
class Receipt:
    vendor_key: str
    receipt_id: str
    revision: str
    purchased_on: date
    vendor_login: str
    items: tuple[ReceiptItem, ...]
    tax_cents: int
    total_cents: int
    tenders: tuple[Tender, ...]
    is_refund: bool
    refund_of: str | None = None
    ship_dates: tuple[date, ...] = ()  # matching evidence, not part of the revision


@dataclass(frozen=True)
class Offset:
    """A part of a receipt paid without a tracked account, such as a reward or a shop card.
    amount_cents is signed against the charge: positive on a purchase, negative on a refund."""

    description: str
    amount_cents: int


@dataclass(frozen=True)
class Charge:
    """The part of a receipt paid as one transaction on one account.

    item_positions None means every item on the receipt. observed is False when the charge
    was inferred from transactions rather than stated by the receipt.

    anchor is the date the "closest transaction" tie-break measures from; None means the purchase
    date. transaction_id is set on an inferred charge: the transaction it was inferred from, which
    matching uses directly instead of searching again by amount.

    offsets are the receipt's other payments when this charge paid the rest: the items are then
    allocated at full price, and each offset is a line of its own, so the lines add up to the
    charge.
    """

    receipt_key: str
    amount_cents: int
    window_start: date
    window_end: date
    card_last4: str | None
    item_positions: tuple[int, ...] | None
    observed: bool
    anchor: date | None = None
    transaction_id: int | None = None
    offsets: tuple[Offset, ...] = ()


@dataclass(frozen=True)
class ChargeResult:
    """What a vendor knows about a receipt's charges.

    "observed" means the charges are known: stated by the receipt, or inferred by
    infer_charges (see Charge.observed). "needs_transactions" asks the engine to gather
    candidate transactions and call infer_charges. "insufficient" carries a reason for review.

    infer_if_unmatched asks for infer_charges when none of the charges above has a candidate
    transaction at all. The stated charge is tried first, so a receipt it explains keeps its key.
    """

    kind: ChargeKind
    charges: tuple[Charge, ...] = ()
    reason: str | None = None
    infer_if_unmatched: bool = False


def charge_key(vendor_key: str, receipt_id: str, index: int) -> str:
    """The key that identifies one charge of one receipt, e.g. "amazon:113-42:0"."""
    return f"{vendor_key}:{receipt_id}:{index}"


def receipt_ref(vendor_key: str, receipt_id: str) -> str:
    """The key that identifies a receipt as a whole, for outcomes that have no charge."""
    return f"{vendor_key}:{receipt_id}"


def receipt_revision(receipt: Receipt) -> str:
    """SHA-256 of the receipt's facts as canonical JSON. The receipt's own revision is ignored."""
    facts = {
        "vendor_key": receipt.vendor_key,
        "receipt_id": receipt.receipt_id,
        "purchased_on": receipt.purchased_on.isoformat(),
        "vendor_login": receipt.vendor_login,
        "items": [
            {
                "position": item.position,
                "description": item.description,
                "amount_cents": item.amount_cents,
                "taxable": item.taxable,
                "quantity": item.quantity,
            }
            for item in sorted(receipt.items, key=lambda item: item.position)
        ],
        "tax_cents": receipt.tax_cents,
        "total_cents": receipt.total_cents,
        "tenders": sorted(
            (
                {"kind": t.kind, "last4": t.last4, "amount_cents": t.amount_cents}
                for t in receipt.tenders
            ),
            key=lambda tender: json.dumps(tender, sort_keys=True),
        ),
        "is_refund": receipt.is_refund,
        "refund_of": receipt.refund_of,
    }
    text = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_receipt(
    *,
    vendor_key: str,
    receipt_id: str,
    purchased_on: date,
    vendor_login: str,
    items: Iterable[ReceiptItem],
    tax_cents: int,
    total_cents: int,
    tenders: Iterable[Tender] = (),
    is_refund: bool = False,
    refund_of: str | None = None,
    ship_dates: Iterable[date] = (),
) -> Receipt:
    """Build a receipt whose revision is computed from its facts, and check its shape.

    ship_dates is evidence for matching and is left out of the revision.

    Raises ValueError for an empty receipt ID or duplicate item positions, and TypeError for
    money that is not integer cents.
    """
    if not receipt_id:
        raise ValueError("a receipt needs a non-empty receipt_id")
    item_tuple = tuple(items)
    positions = [item.position for item in item_tuple]
    if len(set(positions)) != len(positions):
        raise ValueError(f"receipt {receipt_id} repeats an item position")
    for item in item_tuple:
        _require_cents(item.amount_cents, f"item {item.position} amount")
        if isinstance(item.quantity, bool) or not isinstance(item.quantity, int):
            raise TypeError(f"item {item.position} quantity must be an int")
        if item.quantity < 1:
            raise ValueError(f"item {item.position} quantity must be at least 1")
    _require_cents(tax_cents, "tax_cents")
    _require_cents(total_cents, "total_cents")
    tender_tuple = tuple(tenders)
    for tender in tender_tuple:
        if tender.amount_cents is not None:
            _require_cents(tender.amount_cents, "tender amount")
    draft = Receipt(
        vendor_key=vendor_key,
        receipt_id=receipt_id,
        revision="",
        purchased_on=purchased_on,
        vendor_login=vendor_login,
        items=item_tuple,
        tax_cents=tax_cents,
        total_cents=total_cents,
        tenders=tender_tuple,
        is_refund=is_refund,
        refund_of=refund_of,
        ship_dates=tuple(sorted(set(ship_dates))),
    )
    return replace(draft, revision=receipt_revision(draft))


def _require_cents(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be integer cents, not {type(value).__name__}")
