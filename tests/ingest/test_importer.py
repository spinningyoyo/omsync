"""Import matching on an in-memory database. Every statement here is synthetic."""

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    BatchError,
)
from omsync.core.db import Base, make_engine
from omsync.ingest.accounts import create_account, find_account
from omsync.ingest.formats.base import ParsedBalance, ParsedRow, Statement, sha256_of
from omsync.ingest.formats.ofx import OfxFormat
from omsync.ingest.importer import (
    apply_import,
    plan_import,
)
from omsync.ledger.models import (
    TransactionRow,
)

BANK = "testbank"
ACCOUNT_ID = "900000001111"
TRUSTED = OfxFormat(fitid_trusted=True)
IMPORT_TABLES = (
    "statement_file",
    "statement_row",
    "statement_balance",
    "transaction",
    "transaction_row",
    "import_review",
)


@contextmanager
def open_database() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def session() -> Iterator[Session]:
    with open_database() as session:
        yield session


@pytest.fixture
def account_id(session: Session) -> int:
    create_account(
        session,
        institution_key=BANK,
        institution_name="Test Bank",
        statement_account_id=ACCOUNT_ID,
        name="Checking",
        account_type="checking",
        last4="1111",
        idempotency_key="account-1",
    )
    return find_account(session, BANK, ACCOUNT_ID).id


# Helpers


def line(day: str, cents: int, payee: str, fitid: str | None = None) -> tuple:
    return (date.fromisoformat(day), cents, payee, fitid)


def make_statement(
    label: str,
    lines: list[tuple],
    *,
    start: date | None = None,
    end: date | None = None,
    balances: tuple[ParsedBalance, ...] = (),
    account_id: str = ACCOUNT_ID,
) -> Statement:
    rows = tuple(
        ParsedRow(
            position=position,
            posted_on=posted_on,
            amount_cents=cents,
            payee_raw=payee,
            memo_raw=None,
            fitid=fitid,
            raw={"DESCRIPTION": payee, "AMOUNT": str(cents)},
        )
        for position, (posted_on, cents, payee, fitid) in enumerate(lines, start=1)
    )
    return Statement(
        format_key="ofx",
        sha256=sha256_of(label.encode("utf-8")),
        statement_account_id=account_id,
        account_type_hint="checking",
        period_start=start,
        period_end=end,
        rows=rows,
        balances=balances,
    )


def plan(session: Session, statement: Statement, fmt=TRUSTED):
    return plan_import(
        session,
        statement,
        fmt,
        institution_key=BANK,
        stored_path=f"/synthetic/{statement.sha256[:8]}.ofx",
        original_name=f"synthetic-{statement.sha256[:8]}.ofx",
    )


def run(session: Session, statement: Statement, fmt=TRUSTED, key: str | None = None) -> Batch:
    return apply_import(
        session,
        plan(session, statement, fmt),
        idempotency_key=key or f"import:{statement.sha256}",
    )


def count(session: Session, table_name: str) -> int:
    table = Base.metadata.tables[table_name]
    return session.scalar(select(func.count()).select_from(table))


def table_counts(session: Session) -> dict[str, int]:
    names = (*IMPORT_TABLES, "batch", "batch_change", "data_revision")
    return {name: count(session, name) for name in names}


def link_kinds(session: Session) -> Counter:
    return Counter(link.match_kind.value for link in session.scalars(select(TransactionRow)))


def file_a() -> Statement:
    return make_statement(
        "file-a",
        [
            line("2026-10-02", -1250, "GROCER MART", "F1"),
            line("2026-10-03", -899, "COFFEE HOUSE", "F2"),
            line("2026-10-05", -4500, "HARDWARE DEPOT", "F3"),
            line("2026-10-06", -2000, "BOOK NOOK", "F4"),
            line("2026-10-08", -1575, "BAKERY LANE", "F5"),
        ],
        start=date(2026, 10, 1),
        end=date(2026, 10, 10),
        balances=(
            ParsedBalance(as_of=date(2026, 10, 10), balance_cents=123456, kind="ledger"),
            ParsedBalance(as_of=date(2026, 10, 10), balance_cents=120000, kind="available"),
        ),
    )


# Plan, preview, and re-import


def test_reimporting_the_identical_file_is_a_no_op(session, account_id):
    run(session, file_a(), key="first")
    before = table_counts(session)

    again = plan(session, file_a())

    assert again.already_imported is True
    assert again.preview is None
    assert again.account_id == account_id
    assert (again.joined, again.created, again.review) == (0, 0, 0)
    assert table_counts(session) == before
    with pytest.raises(BatchError):
        apply_import(session, again, idempotency_key="second")
    assert table_counts(session) == before


# Identical rows and false merges


def test_identical_same_day_purchases_are_counted_not_merged(session, account_id):
    twins = [line("2026-10-04", -350, "CAFE CORNER"), line("2026-10-04", -350, "CAFE CORNER")]
    run(session, make_statement("twins-1", twins), key="one")
    assert count(session, "transaction") == 2

    run(session, make_statement("twins-2", twins), key="two")
    assert count(session, "transaction") == 2
    assert link_kinds(session) == Counter(created=2, fingerprint=2)

    partial = make_statement("twins-partial", [twins[0]])
    result = plan(session, partial)
    assert (result.joined, result.created, result.review) == (1, 0, 0)
    apply_import(session, result, idempotency_key="three")
    assert count(session, "transaction") == 2

    # Trusted FITIDs name separate purchases. Two partial files with the same date, amount and
    # payee but different FITIDs are two transactions, never one.
    run(
        session,
        make_statement("lunch-1", [line("2026-10-07", -1200, "LUNCH TRUCK", "L-1")]),
        key="four",
    )
    second = plan(
        session, make_statement("lunch-2", [line("2026-10-07", -1200, "LUNCH TRUCK", "L-2")])
    )
    assert (second.joined, second.created, second.review) == (0, 1, 0)
    apply_import(session, second, idempotency_key="five")
    assert count(session, "transaction") == 4
    assert link_kinds(session) == Counter(created=4, fingerprint=3)


# Trusted and untrusted FITIDs


# Correction guard and period


# Unmapped accounts


# Apply, balances, and undo


# Review resolution


# Identifiers and aliases
