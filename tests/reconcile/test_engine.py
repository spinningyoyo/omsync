"""The reconciliation run: determinism, rejected proposals, linked transactions, and no writes."""

from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from omsync.core.db import Base
from omsync.reconcile.engine import run

CONF = "ab12cd34ef"
START = date(2026, 1, 1)
END = date(2026, 12, 31)


def _revision(session: Session) -> int:
    return session.scalar(text("SELECT value FROM data_revision WHERE id = 1"))


def _row_counts(session: Session) -> dict[str, int]:
    return {
        table.name: session.scalar(select(func.count()).select_from(table))
        for table in Base.metadata.sorted_tables
    }


def test_a_run_with_a_joint_settlement_writes_nothing(add_txn, session):
    add_txn("joint_card", date(2026, 3, 5), "1000.00", "PAYMENT THANK YOU")
    add_txn("checking", date(2026, 3, 8), "-500.00", f"Zelle payment to Pat; Conf# {CONF}")
    add_txn("joint", date(2026, 3, 8), "500.00", f"Zelle payment from Pat; Conf# {CONF}")
    session.execute(text("INSERT OR IGNORE INTO data_revision (id, value) VALUES (1, 0)"))
    session.commit()
    counts_before = _row_counts(session)
    revision_before = _revision(session)

    result = run(session, start=START, end=END)

    assert {draft.rule_key for draft in result.drafts} == {"joint_card_bill"}
    assert _row_counts(session) == counts_before
    assert _revision(session) == revision_before
    assert not session.new and not session.dirty
