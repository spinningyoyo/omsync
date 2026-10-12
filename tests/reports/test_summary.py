"""Summary figures: a refund reduces expenses and is not income."""

from datetime import date

from report_support import OCT, Ledger, seed_mixed
from sqlalchemy.orm import Session

from omsync.reports.queries import (
    Period,
    summary,
)


def _figures(session: Session, period: Period = OCT, *, joint_share: bool = True):
    return summary(session, period, joint_share=joint_share).figures


def test_a_refund_reduces_expenses_and_is_not_income(session: Session, ledger: Ledger) -> None:
    seed_mixed(ledger)

    refund_only = Period(date(2026, 10, 12), date(2026, 10, 12))

    figures = _figures(session, refund_only)
    assert figures.income_cents == 0  # the refund sits in a spending category
    assert figures.expenses_cents == -1500  # more came back than went out that day
