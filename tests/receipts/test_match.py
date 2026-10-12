"""Matching: exact amounts, windows, card digits, paid elsewhere, ambiguity, and revisions.

Synthetic data only. The workspace has a checking account ending 1234 and a savings account
ending 5678. Receipts are paid by card 1234 unless a test says otherwise.
"""

from datetime import date

import pytest
from receipt_fakes import (
    TODAY,
    FakeVendor,
    Ledger,
    add_transaction,
    card_receipt,
    item,
)
from sqlalchemy.orm import Session

from omsync.receipts.match import MatchStatus, match_receipts

PURCHASED = date(2026, 9, 10)


@pytest.fixture
def vendor() -> FakeVendor:
    return FakeVendor()


def run(session: Session, vendor: FakeVendor, *receipts, today: date = TODAY):
    return match_receipts(session, vendor, list(receipts), today=today)


def one_item_receipt(receipt_id: str = "R1", total: int = 1000, card: str | None = "1234"):
    return card_receipt(
        receipt_id,
        items=(item(1, "Milk", total),),
        purchased_on=PURCHASED,
        card=card,
    )


def test_two_receipts_that_claim_one_transaction_both_go_to_review(
    session, ledger: Ledger, vendor
) -> None:
    add_transaction(
        session, account_id=ledger.checking, posted_on=date(2026, 9, 12), amount_cents=-1000
    )

    report = run(session, vendor, one_item_receipt("R5"), one_item_receipt("R6"))

    assert [o.status for o in report.outcomes] == [MatchStatus.ambiguous, MatchStatus.ambiguous]
    assert all(
        o.reason == "another receipt charge also matches this transaction" for o in report.outcomes
    )
