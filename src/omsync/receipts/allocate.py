"""Allocation: how one charge is spread across its receipt's categories, to the cent.

The covered items are grouped by category. Tax is spread over the taxable part of each group,
in proportion to that part. Each group's amount plus its tax is then scaled to the charge, so
a partial charge (split shipments) and rounding both come out exact. money.allocate does the
rounding (largest remainder), so the lines always add up to the charge amount.

Nothing is guessed: an item without a category sends the charge to triage.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from omsync.core import money
from omsync.receipts.model import Charge, Receipt, ReceiptItem

CategoryForItem = Callable[[ReceiptItem], int | None]

MEMO_ITEM_LIMIT = 3


class AllocationError(ValueError):
    """The charge cannot be allocated from its receipt's data. Nothing is guessed."""


@dataclass(frozen=True)
class AllocationLine:
    category_id: int
    amount_cents: int  # signed like the charge
    memo: str


@dataclass(frozen=True)
class AllocationResult:
    """Either lines that add up to the charge, or the positions of items that need a category."""

    status: Literal["allocated", "needs_triage"]
    lines: tuple[AllocationLine, ...] = ()
    triage_positions: tuple[int, ...] = ()


def allocate_charge(
    receipt: Receipt, charge: Charge, category_for_item: CategoryForItem
) -> AllocationResult:
    """Spread a charge over the categories of the items it covers.

    One resulting line means no split. Raises AllocationError when the data cannot be spread,
    for example an item that names a position the receipt lacks, or a negative item amount.
    """
    assigned = [(item, category_for_item(item)) for item in _covered_items(receipt, charge)]
    untriaged = tuple(item.position for item, category in assigned if category is None)
    if untriaged:
        return AllocationResult("needs_triage", triage_positions=untriaged)

    groups: dict[int, list[ReceiptItem]] = {}
    for item, category in assigned:
        if category is not None:
            groups.setdefault(category, []).append(item)
    group_ids = list(groups)

    amounts = [sum(item.amount_cents for item in groups[g]) for g in group_ids]
    taxable = [sum(item.amount_cents for item in groups[g] if item.taxable) for g in group_ids]
    tax = _spread_tax(receipt.tax_cents, taxable)
    totals = [amount + share for amount, share in zip(amounts, tax, strict=True)]
    if any(total < 0 for total in totals):
        raise AllocationError("a group's tax is larger than its items, so it cannot be spread")
    if sum(totals) == 0:
        raise AllocationError("the covered items add up to nothing to spread")

    # money.allocate keeps the charge's sign and magnitude, so a refund stays positive.
    shares = money.allocate(charge.amount_cents, totals)
    lines = [
        AllocationLine(group, share, _memo(groups[group]))
        for group, share in zip(group_ids, shares, strict=True)
        if share != 0
    ] or [AllocationLine(group_ids[0], 0, _memo(groups[group_ids[0]]))]

    if sum(line.amount_cents for line in lines) != charge.amount_cents:
        raise RuntimeError("allocation does not add up to the charge; this is a bug")
    return AllocationResult("allocated", lines=tuple(lines))


def _covered_items(receipt: Receipt, charge: Charge) -> list[ReceiptItem]:
    if charge.item_positions is None:
        covered = list(receipt.items)
    else:
        known = {item.position for item in receipt.items}
        missing = sorted(set(charge.item_positions) - known)
        if missing:
            raise AllocationError(f"the charge names items {missing} that the receipt lacks")
        wanted = set(charge.item_positions)
        covered = [item for item in receipt.items if item.position in wanted]
    if not covered:
        raise AllocationError("the charge covers no receipt items")
    for item in covered:
        if item.amount_cents < 0:
            raise AllocationError(
                f"item {item.position} has a negative amount; the vendor should net it "
                "into the item it discounts"
            )
    return sorted(covered, key=lambda item: item.position)


def _spread_tax(tax_cents: int, taxable: list[int]) -> list[int]:
    """Tax goes to the taxable part of each group. With nothing taxable among the covered
    items, no tax is placed here, and the final scaling spreads the charge proportionally."""
    if tax_cents == 0 or sum(taxable) == 0:
        return [0] * len(taxable)
    return money.allocate(tax_cents, taxable)


def _memo(items: list[ReceiptItem]) -> str:
    ordered = sorted(items, key=lambda item: item.position)
    text = "; ".join(item.description for item in ordered[:MEMO_ITEM_LIMIT])
    extra = len(ordered) - MEMO_ITEM_LIMIT
    return f"{text} (+{extra} more)" if extra > 0 else text
