"""Allocation: tax spreading, rounding, partial and negative charges, triage. Synthetic data only.

Category ids are small integers here. The category_for_item callables map descriptions to them.
"""

from collections.abc import Callable
from datetime import date

from receipt_fakes import card_receipt, item

from omsync.receipts.allocate import allocate_charge
from omsync.receipts.model import Charge, ReceiptItem

GROCERIES, HOUSEHOLD, ELECTRONICS = 1, 2, 3


def by_description(description_to_category: dict[str, int]) -> Callable[[ReceiptItem], int | None]:
    return lambda receipt_item: description_to_category.get(receipt_item.description)


def charge_of(amount: int, positions: tuple[int, ...] | None = None) -> Charge:
    return Charge(
        receipt_key="fake:R1:0",
        amount_cents=amount,
        window_start=date(2026, 9, 10),
        window_end=date(2026, 9, 20),
        card_last4="1234",
        item_positions=positions,
        observed=True,
    )


def lines_by_category(result) -> dict[int, int]:
    return {line.category_id: line.amount_cents for line in result.lines}


def test_three_categories_with_tax_and_a_remainder_still_sum_exactly() -> None:
    receipt = card_receipt(
        items=(
            item(1, "Milk", 333),
            item(2, "Soap", 333),
            item(3, "Cable", 334),
        ),
        tax_cents=77,
    )
    # Tax 77 over taxable weights 333, 333, 334 leaves two cents to largest remainders:
    # 26, 25, 26. Each group then carries amount plus tax: 359, 358, 360.
    result = allocate_charge(
        receipt,
        charge_of(-1077),
        by_description({"Milk": GROCERIES, "Soap": HOUSEHOLD, "Cable": ELECTRONICS}),
    )

    assert result.status == "allocated"
    assert lines_by_category(result) == {GROCERIES: -359, HOUSEHOLD: -358, ELECTRONICS: -360}
    assert sum(line.amount_cents for line in result.lines) == -1077
