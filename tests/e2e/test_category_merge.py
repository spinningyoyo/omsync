"""Merging two budgeted categories, end to end: the budget lines are set and the merge is posted
over HTTP the way the browser does it, and undo goes through the CLI.

Two categories may not budget the same month, so that merge is refused and nothing changes. A line
in a month only one of them budgets moves with the merge, and a flex pool line, which belongs to no
category, stays where it is. The Costco food setting moves with the merge too."""

from datetime import date

import pytest
from ledgerkit import Ledger, hidden_fields, plain, web_client

from omsync.core.months import shift_months

MONTH = shift_months(date.today().replace(day=1), -1)
KEY = f"{MONTH:%Y-%m}"
OTHER = f"{shift_months(MONTH, 1):%Y-%m}"


@pytest.fixture
def budgeted(ledger: Ledger, tmp_path):
    ledger.add_account("Checking", last4="1111")
    keywords = tmp_path / "keywords.json"
    keywords.write_text('{"Groceries": ["grocery"], "Dining": ["cafe"]}', encoding="utf-8")
    ledger.cli("keywords", "import", keywords, "--list", "payee", "--apply")
    client = web_client(ledger.data_dir)
    client.post(f"/plan/budget/{KEY}/mode", data={"mode": "category"})
    ids = {n: ledger.query("SELECT id FROM category WHERE name = ?", (n,))[0][0] for n in
           ("Groceries", "Dining")}  # fmt: skip
    return ledger, client, ids


def budget(client, month: str, category_id: int, amount: str) -> None:
    saved = client.post(
        f"/plan/budget/{month}/lines/new", data={"category_id": category_id, "amount": amount}
    )
    assert saved.status_code == 200, saved.text


def merge(client, source: int, target: int):
    """Choose the target in the Delete and move dialog, then confirm it."""
    path = f"/categories/{source}/delete-and-move"
    dialog = client.get(path, params={"target_id": target}).text
    return client.post(path, data={**hidden_fields(dialog, path), "target_id": target})


def costco_food(ledger: Ledger) -> int | None:
    found = ledger.query("SELECT value FROM preference WHERE key = 'costco_food_category'")
    return found[0][0] if found else None


def lines(ledger: Ledger) -> list[tuple]:
    return ledger.query(
        "SELECT b.month, b.kind, c.name, b.amount_cents FROM budget_line b "
        "LEFT JOIN category c ON c.id = b.category_id ORDER BY b.month, b.kind, c.name"
    )


def test_a_merge_is_refused_while_both_categories_budget_the_same_month(budgeted) -> None:
    ledger, client, ids = budgeted
    budget(client, KEY, ids["Groceries"], "200")
    budget(client, KEY, ids["Dining"], "80")
    before = lines(ledger)

    refused = merge(client, ids["Dining"], ids["Groceries"])

    assert refused.status_code >= 400
    assert f"Groceries already has a budget line for {MONTH:%B %Y}" in plain(refused.text)
    assert lines(ledger) == before
    assert ledger.count("category") == 2


def test_a_merge_moves_lines_of_other_months_and_undo_restores_both(budgeted) -> None:
    ledger, client, ids = budgeted
    budget(client, KEY, ids["Groceries"], "200")
    budget(client, OTHER, ids["Dining"], "80")
    client.post(f"/plan/budget/{KEY}/flex-number", data={"amount": "500"})
    client.post(
        "/categories/keywords/item-code",
        data={"list_key": "costco", "code": "E", "category_id": ids["Dining"]},
    )
    before = lines(ledger)
    assert len(before) == 3
    assert costco_food(ledger) == ids["Dining"]

    merged = merge(client, ids["Dining"], ids["Groceries"])

    assert merged.status_code == 200
    assert ledger.count("category") == 1
    assert [(kind, name) for _, kind, name, _ in lines(ledger)] == [
        ("category", "Groceries"),
        ("flex_pool", None),
        ("category", "Groceries"),
    ]
    assert costco_food(ledger) == ids["Groceries"]
    batch = ledger.query("SELECT id FROM batch WHERE kind = 'category.delete_move'")[0][0]

    ledger.cli("batches", "undo", batch)

    assert lines(ledger) == before
    assert ledger.count("category") == 2
    assert costco_food(ledger) == ids["Dining"]
