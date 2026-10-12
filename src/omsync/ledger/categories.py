"""Categories, the keywords and amount rules that assign them, and saved transaction filters."""

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
    money is left out of reports altogether, as transfers and loan payments are."""

    fixed = "fixed"
    flexible = "flexible"
    non_monthly = "non_monthly"
    unassigned = "unassigned"
    income = "income"
    excluded = "excluded"


TYPE_LABELS: dict[CategoryType, str] = {
    CategoryType.fixed: "Fixed",
    CategoryType.flexible: "Flexible",
    CategoryType.non_monthly: "Non-monthly",
    CategoryType.unassigned: "Unassigned",
    CategoryType.income: "Income",
    CategoryType.excluded: "Excluded",
}

# Types whose money is not spending, so a budget cannot plan them.
UNBUDGETED_TYPES = frozenset({CategoryType.income, CategoryType.excluded})

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

    list_key is "unique", "generic", or a receipt vendor's key such as "amazon".
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
    priority: Mapped[int] = mapped_column(default=0, server_default="0")
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())


class AmountRule(Base):
    """Sends a small payee-keyword match to a different category.

    When the winning payee keyword comes from list_key and assigns from_category, and a
    transaction's absolute amount is below below_cents, to_category is assigned instead.
    list_key is "unique" or "generic".
    """

    __tablename__ = "amount_rule"
    __table_args__ = (
        UniqueConstraint(
            "list_key", "from_category_id", "below_cents", name="uq_amount_rule_list_from_limit"
        ),
        CheckConstraint("below_cents > 0", name="ck_amount_rule_limit_positive"),
        CheckConstraint(
            "from_category_id != to_category_id", name="ck_amount_rule_categories_differ"
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    list_key: Mapped[str]
    from_category_id: Mapped[int] = mapped_column(ForeignKey("category.id"))
    below_cents: Mapped[int]
    to_category_id: Mapped[int] = mapped_column(ForeignKey("category.id"))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())


class SavedFilter(Base):
    """A named set of conditions for finding transactions."""

    __tablename__ = "saved_filter"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    conditions: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
