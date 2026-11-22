"""`omsync receipts run`, through the CLI in a tmp data dir.

The database is migrated and seeded with synthetic transactions. The Amazon fixtures are copied
into <data_dir>/receipts/AMAZON/default, the folder of one Amazon provider. The data directory
comes from the environment, so these tests never touch ~/omsync-data."""

import shutil
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from ledgerkit import add_receipt_provider
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from omsync import cli
from omsync.cli import receipts as cli_receipts
from omsync.core.db import database_url, make_engine
from omsync.core.locks import LockBusy, exclusive_lock
from omsync.core.settings import load_settings
from omsync.ingest.sync import SYNC_LOCK_NAME
from omsync.ledger.categories import Category, Keyword
from omsync.ledger.sources import Account, AccountType, Institution
from omsync.ledger.transactions import CategoryOrigin, Transaction

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "receipts" / "amazon"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(data))
    monkeypatch.setenv("OMSYNC_CONFIG", str(tmp_path / "absent.toml"))
    return data


def run(*args: str | Path):
    return CliRunner().invoke(cli.app, [str(arg) for arg in args])


def scalar(data_dir: Path, sql: str) -> int:
    with sqlite3.connect(data_dir / "omsync.db") as connection:
        return connection.execute(sql).fetchone()[0]


SEEDED_TRANSACTIONS = [
    (date(2026, 8, 6), -1196, "AMZN Mktp US"),
    (date(2026, 8, 7), -5775, "AMZN Mktp US"),
    (date(2026, 8, 4), -300, "AMZN Mktp US"),
    (date(2026, 9, 4), -1200, "AMZN Mktp US"),
    (date(2026, 8, 21), 1196, "AMZN Refund"),
    (date(2026, 8, 23), 850, "AMZN Refund"),
    (date(2026, 8, 27), 700, "AMZN Refund"),
]

ORDER_HEADER = (
    "Order ID,Order Date,Order Status,Ship Date,Product Name,ASIN,Unit Price,Unit Price Tax,"
    "Shipping Charge,Total Amount,Shipment Item Subtotal,Shipment Item Subtotal Tax,"
    "Payment Method Type,Original Quantity"
)
REFUND_HEADER = "Order ID,Refund Amount,Refund Date,Reversal Reason,Payment Status"


def money_text(cents: int) -> str:
    return f"{cents / 100:.2f}"


def order_line(
    order_id: str,
    ordered: str,
    shipped: str,
    product: str,
    cents: int,
    payment: str = "Visa - 1111",
    quantity: int = 1,
) -> str:
    """One item row of an order: `quantity` units that cost `cents` together. Dates are ISO days,
    such as 2026-08-05."""
    total = money_text(cents)
    return (
        f"{order_id},{ordered}T10:00:00Z,Closed,{shipped},{product},FAKEASIN,"
        f"{total},0.00,0.00,{total},{total},0.00,{payment},{quantity}"
    )


def refund_line(order_id: str, cents: int, refunded: str, reason: str = "Customer return") -> str:
    return f"{order_id},{money_text(cents)},{refunded}T12:00:00Z,{reason},Completed"


def write_export(data_dir: Path, login: str, orders: list[str], refunds: list[str] = ()) -> None:
    """Write one login's order history, and its refunds when there are any."""
    folder = data_dir / "receipts" / "AMAZON" / "default" / login
    (folder / "Your Amazon Orders").mkdir(parents=True, exist_ok=True)
    (folder / "Your Amazon Orders" / "Order History.csv").write_text(
        "\n".join([ORDER_HEADER, *orders]) + "\n", encoding="utf-8"
    )
    if refunds:
        (folder / "Your Returns & Refunds").mkdir(exist_ok=True)
        (folder / "Your Returns & Refunds" / "Refund Details.csv").write_text(
            "\n".join([REFUND_HEADER, *refunds]) + "\n", encoding="utf-8"
        )


def add_keyword(data_dir: Path, pattern: str, category_name: str) -> None:
    """Add a plain keyword to the amazon list, as a person would after triaging an item."""
    settings = load_settings()
    engine = make_engine(database_url(settings))
    with Session(engine) as session:
        category = session.scalars(select(Category).where(Category.name == category_name)).one()
        session.add(
            Keyword(list_key="amazon", pattern=pattern, is_regex=False, category_id=category.id)
        )
        session.commit()
    engine.dispose()


def seed(data_dir: Path, transactions: list[tuple[date, int, str]] = SEEDED_TRANSACTIONS) -> None:
    """One checking account on card 1111, the amazon keyword list, and the given transactions."""
    settings = load_settings()
    engine = make_engine(database_url(settings))
    with Session(engine) as session:
        institution = Institution(key="example-bank", name="Example Bank")
        session.add(institution)
        session.flush()
        checking = Account(
            institution_id=institution.id, name="Checking", type=AccountType.checking, last4="1111"
        )
        household = Category(name="Household")
        electronics = Category(name="Electronics")
        session.add_all([checking, household, electronics])
        session.flush()
        session.add_all(
            [
                Keyword(
                    list_key="amazon", pattern="Notebook", is_regex=False, category_id=household.id
                ),
                Keyword(
                    list_key="amazon", pattern="Blender", is_regex=False, category_id=household.id
                ),
                Keyword(
                    list_key="amazon", pattern="Soap", is_regex=False, category_id=household.id
                ),
                Keyword(
                    list_key="amazon", pattern="Vase", is_regex=False, category_id=household.id
                ),
                Keyword(
                    list_key="amazon", pattern="Cable", is_regex=False, category_id=electronics.id
                ),
            ]
        )
        for posted_on, amount, payee in transactions:
            session.add(
                Transaction(
                    account_id=checking.id,
                    posted_on=posted_on,
                    amount_cents=amount,
                    payee=payee,
                    payee_raw=payee,
                    memo=None,
                    category_id=None,
                    category_origin=CategoryOrigin.none,
                )
            )
        session.commit()
    engine.dispose()
    add_receipt_provider(data_dir, "amazon")


@pytest.fixture
def ready(data_dir: Path) -> Iterator[Path]:
    """A migrated database with seeded transactions, and the Amazon exports in place."""
    assert run("db", "upgrade").exit_code == 0
    seed(data_dir)
    shutil.copytree(FIXTURES, data_dir / "receipts" / "AMAZON" / "default")
    yield data_dir


def test_run_with_apply_writes_one_batch_and_a_second_run_applies_nothing(ready: Path) -> None:
    applied = run("receipts", "run", "amazon", "--apply")

    assert applied.exit_code == 0, applied.output
    assert "Applied 5 charges as batch 1. Undo with: omsync batches undo 1" in applied.output
    assert scalar(ready, "SELECT COUNT(*) FROM batch") == 1
    assert scalar(ready, "SELECT COUNT(*) FROM link") == 5

    again = run("receipts", "run", "amazon", "--apply")

    assert again.exit_code == 0, again.output
    assert "Nothing to apply." in again.output
    assert scalar(ready, "SELECT COUNT(*) FROM batch") == 1


class FakeFetch:
    """Stands in for the Costco download, so no Chrome or network is used.

    It records each call, and while it runs it checks that the sync lock is held, as the real
    download must hold it. `saved` is what it returns, and `error` is raised instead if set.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.calls: list[tuple[date, date]] = []
        self.saved: list[Path] = []
        self.error: BaseException | None = None
        self.lock_was_held: bool | None = None

    def __call__(self, settings, prompt, *, start: date, end: date) -> list[Path]:
        self.calls.append((start, end))
        try:
            with exclusive_lock(self.data_dir / SYNC_LOCK_NAME):
                self.lock_was_held = False
        except LockBusy:
            self.lock_was_held = True
        if self.error is not None:
            raise self.error
        return self.saved


@pytest.fixture
def fetch(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> FakeFetch:
    fake = FakeFetch(data_dir)
    monkeypatch.setattr(cli_receipts, "fetch_receipts", fake)
    return fake


@pytest.fixture
def database(data_dir: Path) -> Path:
    """A migrated, empty database. Each test seeds its own transactions and exports."""
    assert run("db", "upgrade").exit_code == 0
    return data_dir


def category_of_amount(data_dir: Path, amount_cents: int) -> str | None:
    with sqlite3.connect(data_dir / "omsync.db") as connection:
        row = connection.execute(
            'SELECT c.name FROM "transaction" t LEFT JOIN category c ON c.id = t.category_id '
            "WHERE t.amount_cents = ?",
            (amount_cents,),
        ).fetchone()
    return None if row is None else row[0]


def test_a_partly_applied_split_order_finishes_on_a_later_run(database: Path) -> None:
    # A $29 order charged as $20 and $9. The $9 item has no keyword on the first run.
    seed(
        database,
        [(date(2026, 8, 6), -2000, "AMZN Mktp US"), (date(2026, 8, 8), -900, "AMZN Mktp US")],
    )
    write_export(
        database,
        "Orders_F",
        [
            order_line("F-1", "2026-08-05", "2026-08-06", "Fake Notebook A5", 2000),
            order_line("F-1", "2026-08-05", "2026-08-08", "Fake Lamp", 900),
        ],
    )

    first = run("receipts", "run", "amazon", "--apply")

    assert first.exit_code == 0, first.output
    assert "Applied 1 charges as batch 1." in first.output
    assert "need a category first" in first.output
    assert scalar(database, "SELECT COUNT(*) FROM link") == 1

    add_keyword(database, "Lamp", "Household")
    second = run("receipts", "run", "amazon", "--apply")

    assert second.exit_code == 0, second.output
    assert "Applied 1 charges as batch 2." in second.output
    assert scalar(database, "SELECT COUNT(*) FROM link") == 2
    assert category_of_amount(database, -900) == "Household"

    third = run("receipts", "run", "amazon", "--apply")

    assert third.exit_code == 0, third.output
    assert "Nothing to apply." in third.output
    assert scalar(database, "SELECT COUNT(*) FROM batch") == 2
    assert scalar(database, "SELECT COUNT(*) FROM link") == 2


REFUND_CASES = {
    # Two soaps on one line, and one returned: the refund is one unit of the line.
    "one of two units": (
        [
            order_line("U-1", "2026-08-05", "2026-08-06", "Fake Soap", 2000, quantity=2),
            order_line("U-1", "2026-08-05", "2026-08-06", "Fake Cable", 500),
        ],
        [refund_line("U-1", 1000, "2026-08-20")],
        [(date(2026, 8, 7), -2500, "AMZN Mktp US"), (date(2026, 8, 21), 1000, "AMZN Refund")],
    ),
    # A price adjustment on an order of one product is not a whole number of its units.
    "partial refund of the one product": (
        [order_line("P-1", "2026-08-05", "2026-08-06", "Fake Vase", 3000)],
        [refund_line("P-1", 700, "2026-08-20", reason="Account adjustment")],
        [(date(2026, 8, 7), -3000, "AMZN Mktp US"), (date(2026, 8, 21), 700, "AMZN Refund")],
    ),
}


@pytest.mark.parametrize(
    ("orders", "refunds", "transactions"), REFUND_CASES.values(), ids=REFUND_CASES.keys()
)
def test_a_refund_that_is_not_whole_items_still_names_what_it_refunds(
    database: Path, orders: list[str], refunds: list[str], transactions
) -> None:
    seed(database, transactions)
    write_export(database, "Orders_R", orders, refunds)

    applied = run("receipts", "run", "amazon", "--apply")

    assert "Applied 2 charges as batch 1." in applied.output, applied.output
    assert "need a category first" not in applied.output
    assert category_of_amount(database, transactions[1][1]) == "Household"


ORDER_CASES = {
    # A free item shipped on its own. That shipment charged nothing, so the order is one charge.
    "free item shipped apart": (
        [
            order_line("S-1", "2026-08-05", "2026-08-06", "Fake Soap", 2000),
            order_line("S-1", "2026-08-05", "2026-08-12", "Fake Soap Sample", 0),
        ],
        [(date(2026, 8, 7), -2000, "AMZN Mktp US")],
        "Applied 1 charges as batch 1.",
    ),
    # One shipment, but Amazon charged each item on its own.
    "one shipment charged per item": (
        [
            order_line("C-1", "2026-08-05", "2026-08-06", "Fake Soap", 2000),
            order_line("C-1", "2026-08-05", "2026-08-06", "Fake Cable", 500),
        ],
        [(date(2026, 8, 7), -2000, "AMZN Mktp US"), (date(2026, 8, 7), -500, "AMZN Mktp US")],
        "Applied 2 charges as batch 1.",
    ),
    # Two identical orders and two identical charges: either pairing gives the same categories.
    "twin orders": (
        [
            order_line("T-1", "2026-08-05", "2026-08-06", "Fake Soap", 1200),
            order_line("T-2", "2026-08-05", "2026-08-06", "Fake Soap", 1200),
        ],
        [(date(2026, 8, 7), -1200, "AMZN Mktp US"), (date(2026, 8, 8), -1200, "AMZN Mktp US")],
        "Applied 2 charges as batch 1.",
    ),
    # Another person's card paid an order of two shipments. Nothing of it is in this ledger.
    "another person's card": (
        [
            order_line("O-1", "2026-08-05", "2026-08-06", "Fake Soap", 2000, "Visa - 9999"),
            order_line("O-1", "2026-08-05", "2026-08-09", "Fake Cable", 500, "Visa - 9999"),
        ],
        [(date(2026, 8, 7), -2000, "AMZN Mktp US")],
        "paid elsewhere: 1",
    ),
}


@pytest.mark.parametrize(
    ("orders", "transactions", "expected"), ORDER_CASES.values(), ids=ORDER_CASES.keys()
)
def test_an_order_that_one_charge_cannot_explain_still_finds_its_charges(
    database: Path, orders: list[str], transactions, expected: str
) -> None:
    seed(database, transactions)
    write_export(database, "Orders_O", orders)

    applied = run("receipts", "run", "amazon", "--apply")

    assert expected in applied.output, applied.output
    assert "Review (" not in applied.output
