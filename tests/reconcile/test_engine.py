"""The reconciliation run: determinism, rejected proposals, linked transactions, and no writes."""

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batches import current_revision
from omsync.core.db import Base
from omsync.ledger.counterparties import create_counterparty
from omsync.reconcile.engine import run

ON = date(2026, 3, 2)
CONF = "ab12cd34ef"
START = date(2026, 1, 1)
END = date(2026, 12, 31)


def _row_counts(session: Session) -> dict[str, int]:
    return {
        table.name: session.scalar(select(func.count()).select_from(table))
        for table in Base.metadata.sorted_tables
    }


def test_a_run_with_joint_counterparty_and_p2p_rules_writes_nothing(add_txn, session):
    create_counterparty(
        session,
        name="Sample Sibling",
        kind="person",
        payee_patterns={
            "out": ["Zelle payment to Sample Sibling"],
            "in": ["Zelle payment from Sample Sibling"],
        },
        idempotency_key="engine-counterparty",
    )
    add_txn("joint_card", date(2026, 3, 5), "1000.00", "PAYMENT THANK YOU")
    add_txn("checking", date(2026, 3, 8), "-500.00", f"Zelle payment to Pat; Conf# {CONF}")
    add_txn("joint", date(2026, 3, 8), "500.00", f"Zelle payment from Pat; Conf# {CONF}")
    add_txn("checking", ON, "-40.00", "Zelle payment to Sample Sibling")
    add_txn("savings", ON + timedelta(days=3), "40.00", "Zelle payment from Sample Sibling")
    add_txn("card", ON, "-6000.00", "COSTCO WHSE 123", category="Dining")
    add_txn(
        "savings", ON + timedelta(days=1), "6000.00", "Zelle payment from Pat", memo="costco run"
    )
    current_revision(session)
    session.commit()
    counts_before = _row_counts(session)
    revision_before = current_revision(session)

    result = run(session, start=START, end=END)

    assert {draft.rule_key for draft in result.drafts} >= {"2a", "cp", "p2p"}
    assert _row_counts(session) == counts_before
    assert current_revision(session) == revision_before
    assert not session.new and not session.dirty
