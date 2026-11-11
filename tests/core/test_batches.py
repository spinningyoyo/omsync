"""Batches: preview, apply, and undo, on an in-memory database with synthetic data."""

from datetime import date

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch, BatchChange, BatchState
from omsync.core.batches import (
    BatchError,
    Change,
    UndoConflict,
    apply,
    make_preview,
    snapshot,
    undo,
)
from omsync.core.db import Base, make_engine
from omsync.ledger.models import (
    Account,
    AccountType,
    Category,
    CategoryOrigin,
    Institution,
    Transaction,
)


@pytest.fixture
def engine():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def ledger(session) -> dict[str, int]:
    """Synthetic setup, committed directly: an institution, an account, and a category."""
    institution = Institution(key="test-bank", name="Test Bank")
    session.add(institution)
    session.flush()
    account = Account(institution_id=institution.id, name="Checking", type=AccountType.checking)
    category = Category(name="Groceries")
    session.add_all([account, category])
    session.flush()
    ids = {"account": account.id, "category": category.id}
    session.commit()
    return ids


def transaction_values(ledger: dict[str, int], payee: str = "Bakery", amount: int = -300) -> dict:
    return {
        "account_id": ledger["account"],
        "posted_on": "2026-10-05",
        "amount_cents": amount,
        "payee": payee,
        "payee_raw": payee.upper(),
        "memo": None,
        "category_id": ledger["category"],
        "category_origin": "manual",
    }


def insert_transaction(ledger: dict[str, int], payee: str = "Bakery") -> Change:
    return Change("transaction", None, None, transaction_values(ledger, payee))


def add_transaction(session: Session, ledger: dict[str, int], payee: str = "Cafe") -> int:
    """Seed a transaction directly, as existing data that batches did not write."""
    row = Transaction(
        account_id=ledger["account"],
        posted_on=date(2026, 10, 3),
        amount_cents=-450,
        payee=payee,
        payee_raw=payee.upper(),
        memo=None,
        category_id=ledger["category"],
        category_origin=CategoryOrigin.keyword,
    )
    session.add(row)
    session.flush()
    row_id = row.id
    session.commit()
    return row_id


def revision(session: Session) -> int:
    """The data revision, read from its row. A missing row counts as 0."""
    value = session.scalar(text("SELECT value FROM data_revision WHERE id = 1"))
    return 0 if value is None else value


def row_count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def batch_changes(session: Session, batch_id: int) -> list[BatchChange]:
    return list(
        session.scalars(
            select(BatchChange).where(BatchChange.batch_id == batch_id).order_by(BatchChange.id)
        )
    )


def table_rows(session: Session, name: str) -> list[tuple]:
    table = Base.metadata.tables[name]
    rows = session.execute(select(table).order_by(*table.primary_key.columns)).all()
    return [tuple(row) for row in rows]


def test_a_change_that_breaks_a_constraint_rolls_back_the_whole_batch(session, ledger):
    category_before = snapshot(session, "category", ledger["category"])
    orphan = {**transaction_values(ledger, payee="Orphan"), "account_id": 999}
    preview = make_preview(
        session,
        "bad",
        [
            insert_transaction(ledger, payee="First"),
            Change("category", str(ledger["category"]), {"hidden": False}, {"hidden": True}),
            Change("transaction", None, None, orphan),
        ],
    )

    with pytest.raises(BatchError):
        apply(session, preview, idempotency_key="bad")

    assert row_count(session, Transaction) == 0
    assert snapshot(session, "category", ledger["category"]) == category_before
    assert row_count(session, Batch) == 0
    assert row_count(session, BatchChange) == 0
    assert revision(session) == 0


def test_undo_restores_the_exact_prior_state_for_insert_update_and_delete(session, ledger):
    doomed = add_transaction(session, ledger, payee="Old")
    transactions_before = table_rows(session, "transaction")
    categories_before = table_rows(session, "category")
    preview = make_preview(
        session,
        "mix",
        [
            insert_transaction(ledger, payee="Bakery"),
            Change("category", str(ledger["category"]), {"hidden": False}, {"hidden": True}),
            Change("transaction", str(doomed), snapshot(session, "transaction", doomed), None),
        ],
    )
    batch = apply(session, preview, idempotency_key="mix")
    inserted_id = int(batch_changes(session, batch.id)[0].row_id)

    undone = undo(session, batch.id)

    assert undone.state is BatchState.undone
    assert undone.undone_at is not None
    assert revision(session) == 2
    assert snapshot(session, "transaction", inserted_id) is None
    assert table_rows(session, "transaction") == transactions_before
    assert table_rows(session, "category") == categories_before


def test_undo_is_refused_when_a_row_was_changed_outside_batches(session, ledger):
    applied = apply(
        session,
        make_preview(session, "txn", [insert_transaction(ledger)]),
        idempotency_key="txn",
    )
    txn_id = int(batch_changes(session, applied.id)[0].row_id)
    session.execute(
        text('UPDATE "transaction" SET memo = :memo WHERE id = :id'),
        {"memo": "edited by hand", "id": txn_id},
    )
    session.commit()

    with pytest.raises(UndoConflict) as conflict:
        undo(session, applied.id)

    assert conflict.value.rows == [("transaction", str(txn_id))]
    assert snapshot(session, "transaction", txn_id)["memo"] == "edited by hand"
    assert session.get(Batch, applied.id).state is BatchState.applied
