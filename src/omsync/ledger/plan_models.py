"""Plan tables: monthly budget lines, goals, and recurring series.

Budget mode and expected income are not tables. They are preferences, read and written by
omsync.plan.budgets. Every change to these tables goes through omsync.core.batches, so each one can
be undone.

Every table uses AUTOINCREMENT, so SQLite never reuses a deleted row's id. Undo depends on that.
"""

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from omsync.core.db import Base
from omsync.ledger.enums import string_enum


class BudgetKind(StrEnum):
    """A category line budgets one category. The flex pool line overrides the flex number."""

    category = "category"
    flex_pool = "flex_pool"


class BudgetLine(Base):
    """A budget for one month. `month` is always the first day of the month.

    A category line has a category. The flex pool line has none, and a month has at most one.
    Rollover carries the month's unspent or overspent amount into later months.
    """

    __tablename__ = "budget_line"
    __table_args__ = (
        UniqueConstraint("month", "kind", "category_id", name="uq_budget_line_month_kind_category"),
        # NULLs are distinct in a unique constraint, so a second flex pool line for a month
        # needs this partial index.
        Index(
            "uq_budget_line_flex_pool_month",
            "month",
            unique=True,
            sqlite_where=text("kind = 'flex_pool'"),
        ),
        CheckConstraint("amount_cents >= 0", name="ck_budget_line_amount_nonnegative"),
        CheckConstraint(
            "(kind = 'flex_pool' AND category_id IS NULL)"
            " OR (kind = 'category' AND category_id IS NOT NULL)",
            name="ck_budget_line_category_matches_kind",
        ),
        CheckConstraint("strftime('%d', month) = '01'", name="ck_budget_line_first_of_month"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[date] = mapped_column(Date)
    kind: Mapped[BudgetKind] = mapped_column(string_enum(BudgetKind))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), index=True)
    amount_cents: Mapped[int]
    rollover: Mapped[bool] = mapped_column(default=False, server_default=false())


class Goal(Base):
    """A savings or payoff target with an amount and an optional date, funded by monthly
    contributions. An archived goal no longer counts toward the flex number."""

    __tablename__ = "goal"
    __table_args__ = (
        CheckConstraint("target_cents >= 0", name="ck_goal_target_nonnegative"),
        CheckConstraint("monthly_contribution_cents >= 0", name="ck_goal_contribution_nonnegative"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    target_cents: Mapped[int]
    target_date: Mapped[date | None] = mapped_column(Date)
    linked_account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"), index=True)
    monthly_contribution_cents: Mapped[int]
    color: Mapped[str | None]
    icon: Mapped[str | None]
    archived: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Cadence(StrEnum):
    weekly = "weekly"
    biweekly = "biweekly"
    monthly = "monthly"
    quarterly = "quarterly"
    yearly = "yearly"


class SeriesStatus(StrEnum):
    """suggested is found by detection and waits for you. dismissed is never suggested again."""

    suggested = "suggested"
    confirmed = "confirmed"
    dismissed = "dismissed"


class RecurringSeries(Base):
    """A transaction that repeats on a schedule, such as a bill, a subscription, or a paycheck."""

    __tablename__ = "recurring_series"
    __table_args__ = (
        CheckConstraint(
            "amount_tolerance_cents >= 0", name="ck_recurring_series_tolerance_nonnegative"
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    payee_pattern: Mapped[str]
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), index=True)
    cadence: Mapped[Cadence] = mapped_column(string_enum(Cadence))
    amount_cents: Mapped[int]  # typical amount, signed: a bill is negative, a paycheck positive
    amount_tolerance_cents: Mapped[int] = mapped_column(default=0, server_default="0")
    next_due: Mapped[date | None] = mapped_column(Date)
    last_seen: Mapped[date | None] = mapped_column(Date)
    status: Mapped[SeriesStatus] = mapped_column(string_enum(SeriesStatus))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
