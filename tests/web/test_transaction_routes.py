"""Route tests for the transactions register: the page, cell edits, the split editor, and undo.

The app runs on a temporary data directory with a fake vault. Rows are synthetic and seeded
directly, so no test touches the real data directory.
"""

import re
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from support import sign_in

from omsync.core.batch_models import Batch
from omsync.ledger import register
from omsync.ledger.models import (
    Account,
    AccountType,
    Category,
    CategoryOrigin,
    Institution,
    Split,
    Transaction,
)


def seed(app: FastAPI, *, rows: int = 0) -> dict[str, int]:
    """Two accounts, two categories and one hidden category, and three transactions in the
    checking account plus one in the card. `rows` adds that many more in checking."""
    with Session(app.state.engine) as db:
        bank = Institution(key="bank-a", name="Bank A")
        db.add(bank)
        db.flush()
        checking = Account(
            institution_id=bank.id,
            name="Everyday Checking",
            type=AccountType.checking,
            last4="1234",
        )
        card = Account(
            institution_id=bank.id,
            name="Travel Card",
            type=AccountType.credit_card,
            last4="4321",
        )
        groceries = Category(name="Groceries")
        dining = Category(name="Dining")
        secret = Category(name="Secret", hidden=True)
        db.add_all([checking, card, groceries, dining, secret])
        db.flush()

        def txn(account: Account, on: date, cents: int, payee: str) -> Transaction:
            row = Transaction(
                account_id=account.id,
                posted_on=on,
                amount_cents=cents,
                payee=payee,
                payee_raw=payee.upper(),
                memo=None,
                category_origin=CategoryOrigin.none,
            )
            db.add(row)
            return row

        maple = txn(checking, date(2026, 10, 3), -4250, "Maple Grocery")
        city = txn(checking, date(2026, 10, 2), -9900, "City Power Co")
        oldest = txn(checking, date(2026, 9, 30), -1200, "Bluebird Coffee")
        flight = txn(card, date(2026, 10, 1), -45000, "Blue Sky Travel")
        for n in range(rows):
            txn(checking, date(2025, 1, 1) + timedelta(days=n), -100 - n, f"Synthetic {n:03d}")
        db.commit()
        ids = {
            "checking": checking.id,
            "card": card.id,
            "groceries": groceries.id,
            "dining": dining.id,
            "secret": secret.id,
            "maple": maple.id,
            "city": city.id,
            "oldest": oldest.id,
            "flight": flight.id,
        }
    return ids


def count_batches(app: FastAPI) -> int:
    with Session(app.state.engine) as db:
        return int(db.scalar(select(func.count(Batch.id))) or 0)


def stored(app: FastAPI, txn_id: int) -> Transaction:
    with Session(app.state.engine) as db:
        row = db.get(Transaction, txn_id)
        assert row is not None
        db.expunge(row)
        return row


def split_count(app: FastAPI, txn_id: int) -> int:
    with Session(app.state.engine) as db:
        return int(
            db.scalar(select(func.count(Split.id)).where(Split.transaction_id == txn_id)) or 0
        )


def version_of(app: FastAPI, txn_id: int, field_name: str) -> str:
    """The token a cell of this field, rendered right now, would carry."""
    with Session(app.state.engine) as db:
        return register.field_version(register.get_transaction_row(db, txn_id), field_name)


def row_version_of(app: FastAPI, txn_id: int) -> str:
    """The token a split editor opened on this transaction right now would carry."""
    with Session(app.state.engine) as db:
        return register.row_version(register.get_transaction_row(db, txn_id))


def batch_id_in(body: str) -> int:
    match = re.search(r"/batches/(\d+)/undo", body)
    assert match, "the toast has no Undo button"
    return int(match.group(1))


@pytest.fixture
def client_signed_in(client: TestClient) -> TestClient:
    sign_in(client)
    return client


# Access


# The page


def payees(page: str) -> list[str]:
    return re.findall(r'data-field="payee" data-txn-id="\d+"><span[^>]*>([^<]*)</span></td>', page)


# Cell display and editing


def test_a_save_that_races_another_change_is_a_409_and_writes_nothing(
    client_signed_in: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = seed(app)
    real_make_preview = register.make_preview

    def race_after_preview(session, kind, changes, summary=None):  # type: ignore[no-untyped-def]
        preview = real_make_preview(session, kind, changes, summary)
        session.execute(text("INSERT OR IGNORE INTO data_revision (id, value) VALUES (1, 0)"))
        session.execute(text("UPDATE data_revision SET value = value + 1"))
        return preview

    monkeypatch.setattr(register, "make_preview", race_after_preview)

    response = client_signed_in.post(
        f"/transactions/{ids['maple']}/cell/memo",
        data={"value": "Changed", "version": version_of(app, ids["maple"], "memo")},
    )

    assert response.status_code == 409
    assert "This changed elsewhere, reload." in response.text
    assert "<input" in response.text
    assert stored(app, ids["maple"]).memo is None
    assert count_batches(app) == 0


def test_the_same_key_twice_applies_once(client_signed_in: TestClient, app: FastAPI) -> None:
    ids = seed(app)
    data = {
        "value": "Once",
        "idempotency_key": "double-submit",
        "version": version_of(app, ids["maple"], "memo"),
    }

    first = client_signed_in.post(f"/transactions/{ids['maple']}/cell/memo", data=data)
    second = client_signed_in.post(f"/transactions/{ids['maple']}/cell/memo", data=data)

    assert first.status_code == second.status_code == 200
    assert batch_id_in(first.text) == batch_id_in(second.text)
    assert count_batches(app) == 1


# Undo


# Split editor


def test_split_lines_must_add_up_to_the_transaction(
    client_signed_in: TestClient, app: FastAPI
) -> None:
    ids = seed(app)

    response = client_signed_in.post(
        f"/transactions/{ids['maple']}/split",
        data={
            "amount": ["-20.00", "-20.00"],
            "category": [str(ids["groceries"]), str(ids["dining"])],
            "memo": ["", ""],
            "version": row_version_of(app, ids["maple"]),
        },
    )

    assert response.status_code == 422
    assert "Lines must add up to -$42.50 exactly." in response.text
    assert "split-editor" in response.text
    assert split_count(app, ids["maple"]) == 0
    assert count_batches(app) == 0
