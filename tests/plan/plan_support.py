"""Synthetic ledger rows for the plan tests, and the builder that adds them.

Everything is synthetic and goes into an in-memory database. Nothing reads a real data directory.
The months under test are July to October 2026. The three months before October are July,
August, and September.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from omsync.ledger.categories import CategoryType
from omsync.ledger.models import (
    Account,
    AccountType,
    Category,
    Institution,
    Link,
    LinkKind,
    Split,
    Transaction,
)

AUG = date(2026, 8, 1)
SEP = date(2026, 9, 1)
OCT = date(2026, 10, 1)


@dataclass
class PlanLedger:
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

    def category(
        self,
        name: str,
        *,
        category_type: CategoryType = CategoryType.flexible,
    ) -> int:
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
        """A transaction on a named account. splits are (amount_cents, category name) pairs."""
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

    def link(self, kind: LinkKind, from_id: int, to_id: int, cents: int) -> None:
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


def seed_categories(ledger: PlanLedger) -> None:
    """One category of each flex type, an income category, and an Ignore category."""
    ledger.category("Paycheck", category_type=CategoryType.income)
    ledger.category("Rent", category_type=CategoryType.fixed)
    ledger.category("Gym", category_type=CategoryType.fixed)
    ledger.category("Insurance", category_type=CategoryType.non_monthly)
    ledger.category("Groceries", category_type=CategoryType.flexible)
    ledger.category("Dining", category_type=CategoryType.flexible)
    ledger.category("Pocket cash", category_type=CategoryType.unassigned)
    ledger.category("Card payment", category_type=CategoryType.excluded)


def seed_accounts(ledger: PlanLedger) -> None:
    ledger.account("Checking")
    ledger.account("Joint Checking", joint=True)
