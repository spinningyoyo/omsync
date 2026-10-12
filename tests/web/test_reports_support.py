"""Synthetic ledger rows for the reports route tests. This file has no tests of its own.

October 2026 holds a paycheck, groceries, dining, an uncategorized line, an Ignore line, and a joint
account's groceries bill of 100.01 (odd cents, so joint share has a visible effect). September is a
smaller month, so the changes have something to compare with. Nothing here touches a real data
directory: the app is built on a temporary one.
"""

from dataclasses import dataclass
from datetime import date

from fastapi import FastAPI
from sqlalchemy.orm import Session

from omsync.ledger.models import (
    Account,
    AccountType,
    Category,
    CategoryType,
    Institution,
    Transaction,
)


@dataclass(frozen=True)
class Seeded:
    checking: int
    joint: int
    groceries: int
    dining: int


def seed_reports(app: FastAPI) -> Seeded:
    with Session(app.state.engine) as db:
        bank = Institution(key="bank-a", name="Bank A")
        db.add(bank)
        db.flush()
        checking = Account(
            institution_id=bank.id, name="Everyday Checking", type=AccountType.checking
        )
        joint = Account(
            institution_id=bank.id,
            name="Joint Checking",
            type=AccountType.checking,
            joint=True,
        )
        paycheck = Category(name="Paycheck", type=CategoryType.income)
        groceries = Category(name="Groceries")
        dining = Category(name="Dining")
        card = Category(name="Card payment", type=CategoryType.excluded)
        db.add_all([checking, joint, paycheck, groceries, dining, card])
        db.flush()

        def txn(account: Account, on: date, cents: int, payee: str, category: Category | None):
            db.add(
                Transaction(
                    account_id=account.id,
                    posted_on=on,
                    amount_cents=cents,
                    payee=payee,
                    payee_raw=payee.upper(),
                    category_id=None if category is None else category.id,
                )
            )

        txn(checking, date(2026, 9, 1), 280000, "Employer", paycheck)
        txn(checking, date(2026, 9, 10), -10000, "Grocer", groceries)
        txn(checking, date(2026, 10, 1), 300000, "Employer", paycheck)
        txn(checking, date(2026, 10, 5), -12000, "Grocer", groceries)
        txn(checking, date(2026, 10, 9), -4500, "Diner", dining)
        txn(checking, date(2026, 10, 14), -999, "Unknown shop", None)
        txn(checking, date(2026, 10, 15), -5000, "Card", card)
        txn(joint, date(2026, 10, 3), -10001, "Joint grocer", groceries)
        db.commit()
        return Seeded(
            checking=checking.id,
            joint=joint.id,
            groceries=groceries.id,
            dining=dining.id,
        )
