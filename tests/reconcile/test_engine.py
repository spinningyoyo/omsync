"""The reconciliation run: determinism, rejected proposals, linked transactions, and no writes."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batches import current_revision
from omsync.core.db import Base
from omsync.reconcile.engine import run

CONF = "ab12cd34ef"
START = date(2026, 1, 1)
END = date(2026, 12, 31)


def _row_counts(session: Session) -> dict[str, int]:
    return {
        table.name: session.scalar(select(func.count()).select_from(table))
        for table in Base.metadata.sorted_tables
    }


def test_a_run_with_a_joint_settlement_writes_nothing(add_txn, session):
    add_txn("joint_card", date(2026, 3, 5), "1000.00", "PAYMENT THANK YOU")
    add_txn("checking", date(2026, 3, 8), "-500.00", f"Zelle payment to Pat; Conf# {CONF}")
    add_txn("joint", date(2026, 3, 8), "500.00", f"Zelle payment from Pat; Conf# {CONF}")
    current_revision(session)
    session.commit()
    counts_before = _row_counts(session)
    revision_before = current_revision(session)

    result = run(session, start=START, end=END)

    assert {draft.rule_key for draft in result.drafts} == {"2a"}
    assert _row_counts(session) == counts_before
    assert current_revision(session) == revision_before
    assert not session.new and not session.dirty
