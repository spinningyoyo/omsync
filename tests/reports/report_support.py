"""Synthetic ledger rows for the report tests, and the Ledger builder that adds them.

Everything is synthetic and goes into an in-memory database. Nothing here reads a real data
directory. The named periods are October 2026, the month under test, and the same-length period
before it, August 31 to September 30.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from omsync.ledger.models import (
    Account,
    AccountType,
    Category,
    CategoryType,
    Institution,
    Link,
    LinkKind,
    Split,
    Transaction,
)
from omsync.reports.queries import Period

OCT = Period(date(2026, 10, 1), date(2026, 10, 31))


@dataclass
class Ledger:
    """Adds synthetic rows through the ORM and remembers their IDs by name."""

    session: Session
    institution_id: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    accounts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        institution = Institution(key="bank-a", name="Bank A")
        self.session.add(institution)
        self.session.flush()
        self.institution_id = institution.id

    def account(self, name: str, *, joint: bool = False) -> int:
        row = Account(
            institution_id=self.institution_id,
            name=name,
            type=AccountType.checking,
            joint=joint,
        )
        self.session.add(row)
        self.session.flush()
        self.accounts[name] = row.id
        return row.id

    def category(self, name: str, *, category_type: CategoryType = CategoryType.unassigned) -> int:
        row = Category(name=name, type=category_type)
        self.session.add(row)
        self.session.flush()
        self.categories[name] = row.id
        return row.id

    def txn(
        self,
        account: str,
        on: date,
        cents: int,
        *,
        category: str | None = None,
        payee: str = "Synthetic Shop",
        splits: list[tuple[int, str | None]] | None = None,
    ) -> int:
        """A transaction on a named account. splits are (amount_cents, category name) pairs,
        where a category name of None leaves that split uncategorized."""
        row = Transaction(
            account_id=self.accounts[account],
            posted_on=on,
            amount_cents=cents,
            payee=payee,
            payee_raw=payee.upper(),
            category_id=None if category is None else self.categories[category],
        )
        self.session.add(row)
        self.session.flush()
        for position, (amount, category_name) in enumerate(splits or []):
            self.session.add(
                Split(
                    transaction_id=row.id,
                    position=position,
                    amount_cents=amount,
                    category_id=None if category_name is None else self.categories[category_name],
                )
            )
        self.session.flush()
        return row.id

    def link(self, kind: LinkKind, from_id: int, to_id: int | None, cents: int) -> None:
        self.session.add(
            Link(
                kind=kind,
                amount_cents=cents,
                from_transaction_id=from_id,
                to_transaction_id=to_id,
                evidence={},
            )
        )
        self.session.flush()


def add_categories(ledger: Ledger) -> None:
    ledger.category("Paycheck", category_type=CategoryType.income)
    ledger.category("Groceries")
    ledger.category("Dining")
    ledger.category("Utilities")
    ledger.category("Card payment", category_type=CategoryType.excluded)


def add_accounts(ledger: Ledger) -> None:
    ledger.account("Checking")
    ledger.account("Joint Checking", joint=True)


def seed_mixed(ledger: Ledger) -> None:
    """A month with income, spending, a refund, an uncategorized line, and an Ignore line, plus a
    smaller prior month. October: income 3,000.00, expenses 150.00, uncategorized 9.99 out."""
    add_categories(ledger)
    add_accounts(ledger)
    # Prior month, for the change figures: income 2,800.00, expenses 150.00.
    ledger.txn("Checking", date(2026, 9, 1), 280000, category="Paycheck")
    ledger.txn("Checking", date(2026, 9, 10), -10000, category="Groceries")
    ledger.txn("Checking", date(2026, 9, 11), -5000, category="Dining")
    # October.
    ledger.txn("Checking", date(2026, 10, 1), 300000, category="Paycheck", payee="Employer")
    ledger.txn("Checking", date(2026, 10, 5), -12000, category="Groceries", payee="Grocer")
    ledger.txn("Checking", date(2026, 10, 9), -4500, category="Dining", payee="Diner")
    ledger.txn("Checking", date(2026, 10, 12), 1500, category="Groceries", payee="Grocer refund")
    ledger.txn("Checking", date(2026, 10, 14), -999, payee="Unknown shop")
    ledger.txn("Checking", date(2026, 10, 15), -5000, category="Card payment", payee="Card")
