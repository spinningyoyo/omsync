"""A synthetic receipt vendor and builders for receipts, transactions, and links.

Every value here is invented. The fake vendor names its payee "FAKE MARKET" and its cards by
their last four digits only.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from omsync.ledger.categories import Keyword
from omsync.ledger.transactions import CategoryOrigin, Transaction
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

TODAY = date(2026, 10, 11)
VENDOR = "fake"
PAYEE = "FAKE MARKET"


@dataclass(frozen=True)
class Ledger:
    checking: int  # account id, card ending 1234
    savings: int  # account id, card ending 5678
    groceries: int
    household: int
    electronics: int
    dining: int


def item(position: int, description: str, amount: int, *, taxable: bool = True) -> ReceiptItem:
    return ReceiptItem(
        position=position, description=description, amount_cents=amount, taxable=taxable
    )


def card_receipt(
    receipt_id: str = "R1",
    *,
    items: tuple[ReceiptItem, ...],
    tax_cents: int = 0,
    total_cents: int | None = None,
    purchased_on: date = date(2026, 9, 10),
    card: str | None = "1234",
    is_refund: bool = False,
) -> Receipt:
    """A receipt paid by one card. Its total defaults to the items plus tax."""
    total = (
        total_cents if total_cents is not None else sum(i.amount_cents for i in items) + tax_cents
    )
    tenders = (Tender("card", card, total),) if card is not None else ()
    return make_receipt(
        vendor_key=VENDOR,
        receipt_id=receipt_id,
        purchased_on=purchased_on,
        vendor_login="login-a",
        items=items,
        tax_cents=tax_cents,
        total_cents=total,
        tenders=tenders,
        is_refund=is_refund,
    )


@dataclass
class FakeVendor(BaseVendor):
    """Observed charges come from each receipt's card tenders, with an eleven-day window.

    charge_overrides replaces a receipt's charges(). inference answers infer_charges() for a
    receipt, and inference_calls records the candidates each call was shown.
    """

    key: str = VENDOR
    charge_overrides: dict[str, ChargeResult] = field(default_factory=dict)
    inference: dict[str, ChargeResult] = field(default_factory=dict)
    inference_calls: list[tuple[str, list[CandidateTxn]]] = field(default_factory=list)

    def discover(self, root: Path) -> list[Path]:
        return sorted(root.glob("*.fake"))

    def parse(self, path: Path) -> list[Receipt]:
        raise NotImplementedError("the fake vendor reads no files")

    def is_vendor_payee(self, payee: str) -> bool:
        return "FAKE" in payee.upper()

    def charges(self, receipt: Receipt) -> ChargeResult:
        if receipt.receipt_id in self.charge_overrides:
            return self.charge_overrides[receipt.receipt_id]
        charges = []
        for index, tender in enumerate(receipt.tenders):
            if tender.amount_cents is None:
                continue
            signed = tender.amount_cents if receipt.is_refund else -tender.amount_cents
            charges.append(
                Charge(
                    receipt_key=charge_key(VENDOR, receipt.receipt_id, index),
                    amount_cents=signed,
                    window_start=receipt.purchased_on,
                    window_end=receipt.purchased_on + timedelta(days=10),
                    card_last4=tender.last4,
                    item_positions=None,
                    observed=True,
                )
            )
        return ChargeResult("observed", tuple(charges))

    def infer_charges(self, receipt: Receipt, candidates: list[CandidateTxn]) -> ChargeResult:
        self.inference_calls.append((receipt.receipt_id, list(candidates)))
        return self.inference.get(
            receipt.receipt_id, ChargeResult("insufficient", reason="no inference configured")
        )


def add_transaction(
    session: Session,
    *,
    account_id: int,
    posted_on: date,
    amount_cents: int,
    payee: str = PAYEE,
    category_id: int | None = None,
    origin: CategoryOrigin = CategoryOrigin.none,
) -> int:
    row = Transaction(
        account_id=account_id,
        posted_on=posted_on,
        amount_cents=amount_cents,
        payee=payee,
        payee_raw=payee,
        memo=None,
        category_id=category_id,
        category_origin=origin,
    )
    session.add(row)
    session.flush()
    transaction_id = row.id
    session.commit()
    return transaction_id


def add_keyword(session: Session, list_key: str, pattern: str, category_id: int) -> None:
    """A plain (non-regex) keyword in one list, committed."""
    session.add(
        Keyword(list_key=list_key, pattern=pattern, is_regex=False, category_id=category_id)
    )
    session.commit()
