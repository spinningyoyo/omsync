"""Rollover: the carry into a month is the sum of (budget - actual) over earlier months whose line
has rollover on. Overspending carries as a negative balance. A non-monthly line rolls over by
default in flex mode, unless you chose otherwise."""

from datetime import date

import pytest
from plan_support import AUG, SEP, PlanLedger, seed_accounts, seed_categories
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.ledger.models import Category
from omsync.plan.budgets import rollover_balance, set_line


@pytest.fixture
def budget_ledger(ledger: PlanLedger) -> PlanLedger:
    seed_categories(ledger)
    seed_accounts(ledger)
    return ledger


def _set(
    session: Session,
    ledger: PlanLedger,
    month: date,
    category: str,
    cents: int,
    rollover: bool | None,
) -> None:
    set_line(
        session,
        month,
        ledger.categories[category],
        "category",
        cents,
        rollover,
        idempotency_key=f"{month.isoformat()}-{category}-{cents}-{rollover}",
    )


def _category_id(session: Session, name: str) -> int:
    return session.scalars(select(Category.id).where(Category.name == name)).one()


def test_overspending_carries_a_negative_balance(
    session: Session, budget_ledger: PlanLedger
) -> None:
    _set(session, budget_ledger, AUG, "Groceries", 10_000, True)
    budget_ledger.txn("Checking", date(2026, 8, 9), -13_000, category="Groceries")
    groceries = _category_id(session, "Groceries")

    assert rollover_balance(session, groceries, SEP, joint_share=True) == -3_000
