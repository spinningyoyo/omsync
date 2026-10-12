"""Categorize's origin rules. All data is synthetic and lives in an in-memory database."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy.orm import Session

from omsync.core.db import Base, make_engine
from omsync.ledger.categorize import (
    plan_categorize,
)
from omsync.ledger.models import (
    Account,
    AccountType,
    Category,
    CategoryOrigin,
    Institution,
    Keyword,
    Split,
    Transaction,
)

_CATEGORY_NAMES = ("Coffee", "Dining", "Groceries", "Utilities", "Electronics")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@dataclass
class Ledger:
    session: Session
    account: int
    other_account: int
    categories: dict[str, int]

    def category(self, name: str) -> int:
        return self.categories[name]


@pytest.fixture
def ledger(session: Session) -> Ledger:
    institution = Institution(key="example", name="Example Bank")
    session.add(institution)
    session.flush()
    accounts = [
        Account(institution_id=institution.id, name=name, type=AccountType.checking)
        for name in ("Checking", "Savings")
    ]
    session.add_all(accounts)
    categories = {name: Category(name=name) for name in _CATEGORY_NAMES}
    session.add_all(categories.values())
    session.flush()
    session.commit()
    return Ledger(
        session=session,
        account=accounts[0].id,
        other_account=accounts[1].id,
        categories={name: category.id for name, category in categories.items()},
    )


def keyword(
    ledger: Ledger,
    list_key: str,
    pattern: str,
    category: str,
    *,
    priority: int = 0,
    is_regex: bool = False,
    enabled: bool = True,
) -> int:
    row = Keyword(
        list_key=list_key,
        pattern=pattern,
        is_regex=is_regex,
        category_id=ledger.category(category),
        priority=priority,
        enabled=enabled,
    )
    ledger.session.add(row)
    ledger.session.commit()
    return row.id


def transaction(
    ledger: Ledger,
    payee_raw: str,
    *,
    payee: str | None = None,
    category: str | None = None,
    origin: CategoryOrigin = CategoryOrigin.none,
    account: int | None = None,
    posted_on: date = date(2026, 10, 5),
    splits: int = 0,
) -> int:
    row = Transaction(
        account_id=account if account is not None else ledger.account,
        posted_on=posted_on,
        amount_cents=-600,
        payee=payee if payee is not None else payee_raw.title(),
        payee_raw=payee_raw,
        memo=None,
        category_id=ledger.category(category) if category else None,
        category_origin=origin,
    )
    ledger.session.add(row)
    ledger.session.flush()
    if splits:
        ledger.session.add_all(
            Split(
                transaction_id=row.id,
                position=position,
                amount_cents=amount,
                category_id=ledger.category("Groceries"),
                category_origin=CategoryOrigin.manual,
            )
            for position, amount in enumerate((-100, -500))
        )
    ledger.session.commit()
    return row.id


def state(session: Session, transaction_id: int) -> tuple[str | None, str]:
    """The category name and origin of a transaction, read fresh from the database."""
    session.expire_all()
    row = session.get(Transaction, transaction_id)
    name = session.get(Category, row.category_id).name if row.category_id else None
    return name, row.category_origin.value


# Precedence


# Rule 6: origins and splits


def test_manual_and_receipt_origins_are_never_changed(ledger: Ledger) -> None:
    keyword(ledger, "unique", "cafe", "Coffee")
    manual = transaction(ledger, "CAFE ROMA", category="Groceries", origin=CategoryOrigin.manual)
    receipt = transaction(ledger, "CAFE ROMA", category="Groceries", origin=CategoryOrigin.receipt)

    plan = plan_categorize(ledger.session)

    assert plan.considered == 0
    assert plan.changed == 0
    assert plan.preview is None
    assert state(ledger.session, manual) == ("Groceries", "manual")
    assert state(ledger.session, receipt) == ("Groceries", "receipt")


# Preview and batches


# Conflicts
