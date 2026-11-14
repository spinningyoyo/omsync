"""Seeding and helpers for the register's route tests: accounts, categories, transactions, an import
review made by the real importer, and the request helpers. Synthetic rows only, on a temporary
database."""

import base64
import json
import re
from datetime import date
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from web_support import sign_in

from omsync.core.batch_models import Batch
from omsync.ingest.formats.base import MatchEvidence, ParsedRow, Statement
from omsync.ingest.importer import apply_import, plan_import
from omsync.ledger.accounts import create_account
from omsync.ledger.filters import FilterSpec, to_json
from omsync.ledger.models import (
    Account,
    Category,
    CategoryOrigin,
    ImportReview,
    Split,
    Transaction,
)


def encode(spec: FilterSpec) -> str:
    """The `f` parameter for a spec, as the register writes it."""
    return base64.urlsafe_b64encode(to_json(spec).encode()).decode().rstrip("=")


def f_param(*conditions: object, match: str = "all", search: str | None = None) -> str:
    return encode(FilterSpec(conditions=tuple(conditions), match=match, search=search))  # type: ignore[arg-type]


class FakeFormat:
    """A statement format that has no FITIDs, so every row matches by fingerprint or review."""

    key = "fake"

    def evidence(self, statement: Statement, row: ParsedRow) -> MatchEvidence:
        return MatchEvidence(fitid=None, fitid_trusted=False, fingerprint="unused")


def seed(app: FastAPI, *, review: bool = False) -> dict[str, int]:
    """Two accounts, two categories and a hidden one, and transactions in checking and the card.
    With review=True, a statement import leaves one open review: a row that may correct `oldest`."""
    with Session(app.state.engine) as db:
        for statement_id, name, account_type, last4 in (
            ("acct-1234", "Everyday Checking", "checking", "1234"),
            ("acct-4321", "Travel Card", "credit_card", "4321"),
        ):
            create_account(
                db,
                institution_key="bank-a",
                institution_name="Bank A",
                statement_account_id=statement_id,
                name=name,
                account_type=account_type,
                last4=last4,
                idempotency_key=uuid4().hex,
            )
        checking = db.query(Account).filter_by(last4="1234").one()
        card = db.query(Account).filter_by(last4="4321").one()
        groceries = Category(name="Groceries")
        dining = Category(name="Dining")
        secret = Category(name="Secret", hidden=True)
        db.add_all([groceries, dining, secret])
        db.flush()

        def txn(account: Account, on: date, cents: int, payee: str, **extra: object) -> Transaction:
            row = Transaction(
                account_id=account.id,
                posted_on=on,
                amount_cents=cents,
                payee=payee,
                payee_raw=payee.upper(),
                memo=extra.get("memo"),  # type: ignore[arg-type]
                category_id=extra.get("category_id"),  # type: ignore[arg-type]
                category_origin=extra.get("origin", CategoryOrigin.none),  # type: ignore[arg-type]
            )
            db.add(row)
            db.flush()
            return row

        maple = txn(checking, date(2026, 10, 3), -4250, "Maple Grocery", memo="weekly")
        city = txn(checking, date(2026, 10, 2), -9900, "City Power Co")
        oldest = txn(
            checking,
            date(2026, 9, 30),
            -1200,
            "Bluebird Coffee",
            memo="latte",
            category_id=dining.id,
            origin=CategoryOrigin.keyword,
        )
        flight = txn(card, date(2026, 10, 1), -45000, "Blue Sky Travel")
        harbor = txn(checking, date(2026, 10, 4), -3000, "Harbor Market")
        db.add_all(
            [
                Split(
                    transaction_id=harbor.id,
                    position=0,
                    amount_cents=-1000,
                    category_id=groceries.id,
                    category_origin=CategoryOrigin.manual,
                ),
                Split(
                    transaction_id=harbor.id,
                    position=1,
                    amount_cents=-2000,
                    category_id=dining.id,
                    category_origin=CategoryOrigin.manual,
                ),
            ]
        )
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
            "harbor": harbor.id,
        }
        if review:
            _import_with_review(db, checking.id)
    return ids


def _import_with_review(db: Session, checking_id: int) -> None:
    """One statement row that may correct the 2026-09-30 coffee: the same amount, two days apart,
    inside the statement's period. Import sends it to review with that coffee as a candidate."""
    statement = Statement(
        format_key="fake",
        sha256=uuid4().hex + uuid4().hex,
        statement_account_id="acct-1234",
        account_type_hint=None,
        period_start=date(2026, 9, 29),
        period_end=date(2026, 10, 3),
        rows=(
            ParsedRow(
                position=1,
                posted_on=date(2026, 10, 1),
                amount_cents=-1200,
                payee_raw="BLUEBIRD COFFEE #2",
                memo_raw=None,
                fitid=None,
                raw={"line": "1"},
            ),
        ),
        balances=(),
    )
    plan = plan_import(
        db,
        statement,
        FakeFormat(),  # type: ignore[arg-type]
        institution_key="bank-a",
        stored_path="statements/bank-a/march.csv",
        original_name="march.csv",
    )
    assert plan.review == 1, plan
    apply_import(db, plan, idempotency_key=uuid4().hex)
    assert db.scalar(select(func.count(ImportReview.id))) == 1
    assert checking_id


def count_batches(app: FastAPI, kind: str | None = None) -> int:
    with Session(app.state.engine) as db:
        query = select(func.count(Batch.id))
        if kind is not None:
            query = query.where(Batch.kind == kind)
        return int(db.scalar(query) or 0)


def stored_transaction(app: FastAPI, txn_id: int) -> Transaction:
    with Session(app.state.engine) as db:
        row = db.get(Transaction, txn_id)
        assert row is not None
        db.expunge(row)
        return row


def batch_id_in(body: str) -> int:
    match = re.search(r"/batches/(\d+)/undo", body)
    assert match, "the toast has no Undo button"
    return int(match.group(1))


def revision_in(body: str) -> int:
    match = re.search(r'name="revision" value="(\d+)"', body)
    assert match, "the preview has no revision"
    return int(match.group(1))


def events(response_headers: dict[str, str]) -> dict[str, object]:
    return json.loads(response_headers.get("hx-trigger", "{}"))


def signed_in_client(client: TestClient) -> TestClient:
    sign_in(client)
    return client
