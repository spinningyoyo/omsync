"""The Reconcile page: a decision and its undo, and the stale-proposal message.

All data is synthetic and lives in a temporary database."""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from web_support import sign_in

from omsync.core.db import Base
from omsync.ledger.categories import Category, Keyword
from omsync.ledger.links import Link
from omsync.ledger.models import Account, AccountType, Institution, Transaction
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
from omsync.reconcile.engine import run
from omsync.reconcile.model import signature

START, END = date(2026, 7, 1), date(2026, 9, 30)
RANGE = "start=2026-07-01&end=2026-09-30"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "statements"
ON = date(2026, 8, 3)
BATCH_LINK = re.compile(r"/batches/(\d+)/undo\?start=2026-07-01&amp;end=2026-09-30")


@pytest.fixture
def ledger(app: FastAPI) -> dict[str, int]:
    """Synthetic accounts and August 2026 transactions:

    - a Zelle pair with one confirmation, same day, which proposes an auto transfer;
    - a Zelle inflow with no debit, which is listed as not matched;
    - an ordinary purchase, which proposes nothing.
    """
    with Session(app.state.engine) as db:
        bank = Institution(key="bank-a", name="Bank A")
        db.add(bank)
        db.flush()
        accounts = {
            "checking": Account(
                institution_id=bank.id, name="Checking", type=AccountType.checking, last4="1111"
            ),
            "savings": Account(
                institution_id=bank.id, name="Savings", type=AccountType.savings, last4="2222"
            ),
            "card": Account(
                institution_id=bank.id, name="Card", type=AccountType.credit_card, last4="3333"
            ),
        }
        db.add_all(accounts.values())
        db.add_all([Category(name="Transfer"), Category(name="Shopping")])
        db.flush()
        ids: dict[str, int] = {key: account.id for key, account in accounts.items()}

        def add(account: str, cents: int, payee: str, on: date = ON) -> int:
            txn = Transaction(
                account_id=accounts[account].id,
                posted_on=on,
                amount_cents=cents,
                payee=payee,
                payee_raw=payee,
                memo=None,
            )
            db.add(txn)
            db.flush()
            return txn.id

        ids["zelle_out"] = add("checking", -10_000, "Zelle payment to Pat; Conf# vq9s86kwm")
        ids["zelle_in"] = add("card", 10_000, "Zelle payment from Pat; Conf# vq9s86kwm")
        ids["solo_in"] = add("checking", 5_000, "Zelle payment from Lee", on=date(2026, 8, 5))
        ids["cafe"] = add("card", -3_000, "Corner Cafe")
        db.commit()
    return ids


def _count_all(app: FastAPI) -> dict[str, int]:
    with Session(app.state.engine) as db:
        return {
            table.name: db.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }


def _sig(app: FastAPI, rule: str) -> str:
    with Session(app.state.engine) as db:
        drafts = [d for d in run(db, start=START, end=END).drafts if d.rule_key == rule]
    assert len(drafts) == 1, rule
    return signature(drafts[0])


def _batch_id(html: str) -> int:
    match = BATCH_LINK.search(html)
    assert match, "no undo toast in the reply"
    return int(match.group(1))


def test_approve_one_proposal_then_undo_returns_it(
    client: TestClient, app: FastAPI, ledger: dict
) -> None:
    sign_in(client)
    sig = _sig(app, "zelle_same_confirmation")

    response = client.post(
        "/reconcile/approve",
        data={
            "start": "2026-07-01",
            "end": "2026-09-30",
            "signature": sig,
            "idempotency_key": "a1",
        },
    )

    assert response.status_code == 200
    assert "Approved 1 proposal(s)" in response.text
    assert 'id="reconcile-results"' in response.text
    with Session(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(Link)) == 1
    batch_id = _batch_id(response.text)

    undone = client.post(f"/batches/{batch_id}/undo?start=2026-07-01&end=2026-09-30")

    assert undone.status_code == 200
    assert "Undone" in undone.text
    assert 'id="reconcile-results"' in undone.text
    assert _sig(app, "zelle_same_confirmation") == sig  # the proposal is back
    with Session(app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(Link)) == 0


def test_a_stale_proposal_says_to_run_again_and_writes_nothing(
    client: TestClient, app: FastAPI, ledger: dict
) -> None:
    sign_in(client)
    sig = _sig(app, "zelle_same_confirmation")
    with Session(app.state.engine) as db:
        db.get(Transaction, ledger["zelle_in"]).amount_cents = 9_000  # the pair no longer agrees
        db.commit()
    before = _count_all(app)

    response = client.post(
        "/reconcile/approve",
        data={
            "start": "2026-07-01",
            "end": "2026-09-30",
            "signature": sig,
            "idempotency_key": "s1",
        },
    )

    assert response.status_code == 409
    assert "Results changed since you ran this; run again" in response.text
    assert _count_all(app) == before


class FakeVendor(BaseVendor):
    """A vendor whose one receipt is paid on card 3333 for $30.00 at a payee named FAKE MARKET."""

    key = "fake"

    def discover(self, root: Path) -> list[Path]:
        return sorted(root.glob("*.fake"))

    def parse(self, path: Path) -> list[Receipt]:
        return [
            make_receipt(
                vendor_key=self.key,
                receipt_id="F1",
                purchased_on=date(2026, 8, 10),
                vendor_login="login-a",
                items=(
                    ReceiptItem(position=1, description="Widget", amount_cents=3000, taxable=False),
                ),
                tax_cents=0,
                total_cents=3000,
                tenders=(Tender("card", "3333", 3000),),
            )
        ]

    def is_vendor_payee(self, payee: str) -> bool:
        return "FAKE" in payee.upper()

    def charges(self, receipt: Receipt) -> ChargeResult:
        charges = tuple(
            Charge(
                receipt_key=charge_key(self.key, receipt.receipt_id, index),
                amount_cents=-tender.amount_cents,
                window_start=receipt.purchased_on,
                window_end=receipt.purchased_on + timedelta(days=10),
                card_last4=tender.last4,
                item_positions=None,
                observed=True,
            )
            for index, tender in enumerate(receipt.tenders)
            if tender.amount_cents is not None
        )
        return ChargeResult("observed", charges)


# Amazon receipts from synthetic order exports, applied through the real vendor and routes.

ORDER_HEADER = (
    "Order ID,Order Date,Order Status,Ship Date,Product Name,ASIN,Unit Price,Unit Price Tax,"
    "Shipping Charge,Total Amount,Shipment Item Subtotal,Shipment Item Subtotal Tax,"
    "Payment Method Type"
)


@pytest.fixture
def amazon(app: FastAPI) -> Session:
    """A Groceries keyword for the widget, and an Amazon export folder to write orders into."""
    with Session(app.state.engine) as db:
        groceries = Category(name="Groceries")
        db.add(groceries)
        db.flush()
        db.add(Keyword(list_key="amazon", pattern="WIDGET", category_id=groceries.id))
        db.commit()
    return app.state.settings
