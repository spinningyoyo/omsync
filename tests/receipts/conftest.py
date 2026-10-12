"""Shared fixtures: an in-memory database with a synthetic ledger, and an isolated vendor registry.

Nothing here touches the real data directory.
"""

from collections.abc import Iterator

import pytest
from receipt_fakes import Ledger
from sqlalchemy.orm import Session

from omsync.core.db import Base, make_engine
from omsync.ledger.categories import Category
from omsync.ledger.sources import Account, AccountType, Institution

_CATEGORY_NAMES = ("Groceries", "Household", "Electronics", "Dining")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def ledger(session: Session) -> Ledger:
    institution = Institution(key="example-bank", name="Example Bank")
    session.add(institution)
    session.flush()
    checking = Account(
        institution_id=institution.id, name="Checking", type=AccountType.checking, last4="1234"
    )
    savings = Account(
        institution_id=institution.id, name="Savings", type=AccountType.savings, last4="5678"
    )
    categories = {name: Category(name=name) for name in _CATEGORY_NAMES}
    session.add_all([checking, savings, *categories.values()])
    session.flush()
    session.commit()
    return Ledger(
        checking=checking.id,
        savings=savings.id,
        groceries=categories["Groceries"].id,
        household=categories["Household"].id,
        electronics=categories["Electronics"].id,
        dining=categories["Dining"].id,
    )
