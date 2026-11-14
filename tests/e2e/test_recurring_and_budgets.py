"""Recurring bills and budgets, end to end: statements go in through the CLI, and the pages are
driven over HTTP the way the browser does it.

Both pages are derived from the ledger. Neither may change it, so the ledger total in integer
cents is asserted before and after every decision."""

from datetime import date

import pytest
from ledgerkit import Ledger, Row, hidden_fields, plain, web_client

from omsync.core.months import shift_months


def monthly(count: int, day: int) -> list[date]:
    """The last `count` monthly dates on `day`, none in the future, oldest first."""
    latest = date.today().replace(day=day)
    if latest > date.today():
        latest = shift_months(latest, -1)
    return [shift_months(latest, -n) for n in range(count)][::-1]


@pytest.fixture
def subscribed(ledger: Ledger) -> Ledger:
    """Five monthly streaming charges, two power bills (too few to suggest), and one coffee."""
    ledger.add_account("Checking", last4="1111")
    rows = [Row(d, -1599, "ACME STREAMING", f"s{i}") for i, d in enumerate(monthly(5, 5))]
    rows += [Row(d, -8000, "CITY POWER", f"p{i}") for i, d in enumerate(monthly(2, 12))]
    rows += [Row(monthly(1, 20)[0], -450, "ACME CAFE", "c1")]
    ledger.statement("Checking", rows)
    ledger.import_all()
    return ledger


def test_only_a_repeated_charge_is_suggested_and_confirming_it_changes_no_money(
    subscribed: Ledger,
) -> None:
    total = subscribed.total_cents()
    client = web_client(subscribed.data_dir)
    drawer = client.get("/recurring/review").text

    assert "ACME STREAMING" in drawer and "1 of 1" in drawer  # power and coffee are not offered
    assert "-$15.99" in plain(drawer)

    confirmed = client.post(
        "/recurring/suggestions/confirm",
        data={**hidden_fields(drawer, "/recurring/suggestions/confirm"), "index": "0"},
    )

    assert confirmed.status_code == 200
    assert subscribed.query("SELECT cadence, status FROM recurring_series") == [
        ("monthly", "confirmed")
    ]
    assert "ACME STREAMING" in plain(client.get("/recurring").text)
    assert subscribed.total_cents() == total  # a bill is a plan, not ledger money
    assert "ACME STREAMING" not in client.get("/recurring/review").text  # asked once


def test_dismissing_a_suggestion_is_remembered(subscribed: Ledger) -> None:
    client = web_client(subscribed.data_dir)
    drawer = client.get("/recurring/review").text

    dismissed = client.post(
        "/recurring/suggestions/dismiss",
        data={**hidden_fields(drawer, "/recurring/suggestions/dismiss"), "index": "0"},
    )

    assert dismissed.status_code == 200
    assert subscribed.query("SELECT status FROM recurring_series") == [("dismissed",)]
    subscribed.statement(
        "Checking", [Row(monthly(1, 25)[0], -1599, "ACME STREAMING", "s-new")], file="more.ofx"
    )
    subscribed.import_all()  # a new charge in the dismissed series asks nothing
    assert "ACME STREAMING" not in client.get("/recurring/review").text


def test_a_confirmed_series_can_be_undone(subscribed: Ledger) -> None:
    client = web_client(subscribed.data_dir)
    drawer = client.get("/recurring/review").text
    client.post(
        "/recurring/suggestions/confirm",
        data={**hidden_fields(drawer, "/recurring/suggestions/confirm"), "index": "0"},
    )
    batch = subscribed.query("SELECT id FROM batch WHERE kind LIKE 'recurring%' ORDER BY id DESC")
    assert batch

    subscribed.cli("batches", "undo", batch[0][0])

    assert subscribed.query("SELECT COUNT(*) FROM recurring_series") == [(0,)]


# Budgets

MONTH = shift_months(date.today().replace(day=1), -1)  # a complete month
KEY = f"{MONTH:%Y-%m}"
NEXT = f"{shift_months(MONTH, 1):%Y-%m}"
# Groceries spending is 4550 + 1000 - 1000 (a refund) = 4550 cents.
SPENT = 4550


@pytest.fixture
def budgeting(ledger: Ledger, tmp_path):
    ledger.add_account("Checking", last4="1111")
    rows = [
        Row(MONTH.replace(day=10), -4550, "ACME GROCERY", "g1"),
        Row(MONTH.replace(day=12), -1000, "ACME GROCERY", "g2"),
        Row(MONTH.replace(day=16), 1000, "ACME GROCERY REFUND", "g3"),
        Row(MONTH.replace(day=14), -2500, "ACME CAFE", "c1"),
        Row(MONTH.replace(day=15), 300000, "ACME PAYROLL", "i1"),
    ]
    ledger.statement("Checking", rows)
    ledger.import_all()
    keywords = tmp_path / "keywords.json"
    keywords.write_text(
        '{"Groceries": ["grocery"], "Coffee": ["cafe"], "Paycheck": ["payroll"]}', encoding="utf-8"
    )
    ledger.cli("keywords", "import", keywords, "--list", "payee", "--apply")
    ledger.cli("categorize", "--apply")
    client = web_client(ledger.data_dir)
    client.post(f"/plan/budget/{KEY}/mode", data={"mode": "category"})
    groceries = ledger.query("SELECT id FROM category WHERE name = 'Groceries'")[0][0]
    return ledger, client, groceries


@pytest.mark.parametrize(
    ("budget_cents", "left"),
    [
        pytest.param(20000, "$154.50", id="under budget"),
        pytest.param(4550, "$0.00", id="exactly on budget"),
        pytest.param(4000, "-$5.50", id="over budget"),
    ],
)
def test_left_is_the_budget_minus_the_reported_spending(
    budgeting, budget_cents: int, left: str
) -> None:
    ledger, client, groceries = budgeting
    total = ledger.total_cents()
    left_cents = budget_cents - SPENT
    assert (
        left
        == f"{'-' if left_cents < 0 else ''}${abs(left_cents) // 100}.{abs(left_cents) % 100:02d}"
    )

    saved = client.post(
        f"/plan/budget/{KEY}/lines/new",
        data={"category_id": groceries, "amount": f"{budget_cents / 100:.2f}"},
    )

    assert saved.status_code == 200
    assert ledger.query("SELECT amount_cents FROM budget_line") == [(budget_cents,)]
    assert f"Left {left}" in plain(client.get(f"/plan/budget/{KEY}").text)
    assert ledger.total_cents() == total  # a budget plans money, it never moves it


def test_a_copy_fills_only_an_empty_month_and_can_be_undone(budgeting) -> None:
    ledger, client, groceries = budgeting
    client.post(f"/plan/budget/{KEY}/lines/new", data={"category_id": groceries, "amount": "200"})

    first = client.post(f"/plan/budget/{NEXT}/copy", data={})

    assert first.status_code == 200
    assert ledger.query("SELECT amount_cents FROM budget_line ORDER BY month") == [(20000,)] * 2
    # A second copy would overwrite the month, so it is refused and nothing changes.
    again = client.post(f"/plan/budget/{NEXT}/copy", data={})
    assert again.status_code >= 400
    assert ledger.count("budget_line") == 2

    batch = ledger.query("SELECT id FROM batch WHERE kind LIKE 'budget%' ORDER BY id DESC")[0][0]
    ledger.cli("batches", "undo", batch)
    assert ledger.count("budget_line") == 1
