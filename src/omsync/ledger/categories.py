"""Categories, the keywords and rules that assign them, and saved transaction filters."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from omsync.core.db import Base
from omsync.ledger.enums import string_enum


class CategoryType(StrEnum):
    """What a category is for. Fixed, flexible, and non-monthly are the types of expense a budget
    plans; unassigned is an expense not yet classified. Income feeds income in reports. Excluded
    money is left out of reports altogether, as transfers and loan payments are. Owed money is left
    out of reports like excluded money, and is what contact balances count: a loan, or money held
    for a contact."""

    fixed = "fixed"
    flexible = "flexible"
    non_monthly = "non_monthly"
    unassigned = "unassigned"
    income = "income"
    excluded = "excluded"
    owed = "owed"


TYPE_LABELS: dict[CategoryType, str] = {
    CategoryType.fixed: "Fixed",
    CategoryType.flexible: "Flexible",
    CategoryType.non_monthly: "Non-monthly",
    CategoryType.unassigned: "Unassigned",
    CategoryType.income: "Income",
    CategoryType.excluded: "Excluded",
    CategoryType.owed: "Owed",
}

# Types whose money reports leave out. Only contact balances read the owed ones.
NOT_IN_REPORTS = frozenset({CategoryType.excluded, CategoryType.owed})

# Types whose money is not spending, so a budget cannot plan them.
UNBUDGETED_TYPES = frozenset({CategoryType.income, CategoryType.excluded, CategoryType.owed})

# The Lucide icon a category shows until one is chosen.
DEFAULT_CATEGORY_ICON = "shapes"


class Category(Base):
    """What a transaction or split was for."""

    __tablename__ = "category"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    type: Mapped[CategoryType] = mapped_column(
        string_enum(CategoryType),
        default=CategoryType.unassigned,
        server_default=CategoryType.unassigned.value,
    )
    hidden: Mapped[bool] = mapped_column(default=False, server_default=false())
    icon: Mapped[str | None] = mapped_column(default=None)


class Keyword(Base):
    """A pattern that assigns a category to a payee or receipt item.

    list_key is "payee", for who was paid, or a receipt vendor's key such as "amazon".
    """

    __tablename__ = "keyword"
    __table_args__ = (
        UniqueConstraint("list_key", "pattern", name="uq_keyword_list_pattern"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    list_key: Mapped[str]
    pattern: Mapped[str]
    is_regex: Mapped[bool] = mapped_column(default=True, server_default=true())
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), index=True)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())


class RuleOtherSide(StrEnum):
    """Who is on the other side of a payment that a rule reads. person is a payment with a person
    (see omsync.ledger.other_side.PersonSides); contact is the contact stored on the rule; and
    owner_account is a payment whose other side is you, one of your own accounts."""

    person = "person"
    contact = "contact"
    owner_account = "owner_account"


class CategoryRule(Base):
    """Sets a category on a transaction no payee keyword matched, when all of its conditions hold.

    A keyword always beats a rule, so what was bought beats how it was paid. Rules are tried in
    position order, lowest first, and the first that holds wins; rules never chain. A condition
    that is not set does not matter, and at least one must be set. paid_by holds the payment
    methods (see omsync.ledger.payment_methods) separated by spaces, and holds when the bank's
    wording names one of them. below_cents is strict and at_least_cents inclusive, both on the
    absolute amount, so a refund follows its purchase. other_side reads who is on the other side,
    and contact_id and account_id name a contact and an account of the transaction.
    """

    __tablename__ = "category_rule"
    __table_args__ = (
        CheckConstraint(
            "paid_by IS NOT NULL OR below_cents IS NOT NULL"
            " OR at_least_cents IS NOT NULL OR other_side IS NOT NULL OR account_id IS NOT NULL",
            name="ck_category_rule_has_condition",
        ),
        CheckConstraint("below_cents IS NULL OR below_cents > 0", name="ck_category_rule_below"),
        CheckConstraint(
            "at_least_cents IS NULL OR at_least_cents > 0", name="ck_category_rule_at_least"
        ),
        CheckConstraint(
            "below_cents IS NULL OR at_least_cents IS NULL OR below_cents > at_least_cents",
            name="ck_category_rule_range",
        ),
        CheckConstraint(
            "CASE WHEN other_side = 'contact' THEN contact_id IS NOT NULL"
            " ELSE contact_id IS NULL END",
            name="ck_category_rule_contact",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    position: Mapped[int]
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
    paid_by: Mapped[str | None]
    below_cents: Mapped[int | None]
    at_least_cents: Mapped[int | None]
    other_side: Mapped[RuleOtherSide | None] = mapped_column(string_enum(RuleOtherSide))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contact.id"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"))
    to_category_id: Mapped[int] = mapped_column(ForeignKey("category.id"))


class SavedFilter(Base):
    """A named set of conditions for finding transactions."""

    __tablename__ = "saved_filter"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    conditions: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
