"""Shared fixtures: an in-memory database with synthetic accounts and helpers that add transactions.

Nothing here touches the real data directory.
"""

from collections.abc import Callable, Iterator
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from omsync.core.db import Base, make_engine
from omsync.core.money import to_cents
from omsync.ledger.models import Account, AccountType, Category, Institution, Transaction
from omsync.ledger.preferences import Preference  # noqa: F401  (registers the preference table)
from omsync.reconcile.pool import Pool, load_pool
from omsync.reconcile.rules.base import Emitter


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def accounts(session: Session) -> dict[str, Account]:
    """Five synthetic accounts at one bank, and two categories, all committed."""
    bank = Institution(key="test-bank", name="Test Bank")
    session.add(bank)
    session.flush()
    rows = {
        "checking": Account(
            institution_id=bank.id, name="Checking", type=AccountType.checking, last4="1111"
        ),
        "savings": Account(
            institution_id=bank.id, name="Savings", type=AccountType.savings, last4="2222"
        ),
        "card": Account(
            institution_id=bank.id, name="Card", type=AccountType.credit_card, last4="3333"
        ),
        "card2": Account(
            institution_id=bank.id, name="Card Two", type=AccountType.credit_card, last4="4444"
        ),
        "joint": Account(
            institution_id=bank.id,
            name="Joint Checking",
            type=AccountType.checking,
            last4="5555",
            joint=True,
        ),
        "joint_card": Account(
            institution_id=bank.id,
            name="Joint Card",
            type=AccountType.credit_card,
            last4="6666",
            joint=True,
        ),
    }
    session.add_all(rows.values())
    session.add_all(
        [
            Category(name="Shopping"),
            Category(name="Dining"),
            Category(name="Family_Joint"),
            Category(name="Transfer"),
        ]
    )
    session.commit()
    return rows


@pytest.fixture
def add_txn(session: Session, accounts: dict[str, Account]) -> Callable[..., int]:
    """Return a function that adds one transaction and returns its ID.

    amount is dollars as a string, such as "-50.00". payee defaults to payee_raw.
    """

    def _add(
        account: str,
        on: date,
        amount: str,
        payee_raw: str,
        *,
        payee: str | None = None,
        category: str | None = None,
        memo: str | None = None,
    ) -> int:
        category_id = None
        if category is not None:
            category_id = session.scalars(
                select(Category.id).where(Category.name == category)
            ).one()
        txn = Transaction(
            account_id=accounts[account].id,
            posted_on=on,
            amount_cents=to_cents(amount),
            payee=payee or payee_raw,
            payee_raw=payee_raw,
            memo=memo,
            category_id=category_id,
        )
        session.add(txn)
        session.commit()
        return txn.id

    return _add


@pytest.fixture
def pool_for(session: Session) -> Callable[..., Pool]:
    """Return a function that loads the pool for 2026, the year the synthetic data uses."""

    def _pool(*, include_linked: bool = False) -> Pool:
        return load_pool(
            session,
            start=date(2026, 1, 1),
            end=date(2026, 12, 31),
            include_linked=include_linked,
        )

    return _pool


@pytest.fixture
def run_rule() -> Callable[..., Emitter]:
    """Return a function that runs one rule over a pool and returns its emitter."""

    def _run(rule: Callable[[Pool, Emitter], None], pool: Pool, rejected=None) -> Emitter:
        emit = Emitter(rejected)
        rule(pool, emit)
        return emit

    return _run
