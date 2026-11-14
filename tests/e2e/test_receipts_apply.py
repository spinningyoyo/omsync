"""Amazon receipts applied to imported charges, end to end through the CLI.

The charges come in as a synthetic bank statement, and the receipts are the synthetic Amazon
exports under tests/fixtures. A receipt explains a charge. It never creates, removes or changes
money, so the ledger total and every transaction amount are asserted in integer cents."""

import shutil
from datetime import date
from pathlib import Path

import pytest
from ledgerkit import Ledger, Row

EXPORTS = Path(__file__).resolve().parents[1] / "fixtures" / "receipts" / "amazon"
CHARGES = [
    (date(2026, 8, 6), -1196, "AMZN Mktp US"),
    (date(2026, 8, 7), -5775, "AMZN Mktp US"),
    (date(2026, 8, 4), -300, "AMZN Mktp US"),
    (date(2026, 9, 4), -1200, "AMZN Mktp US"),
    (date(2026, 8, 21), 1196, "AMZN Refund"),
    (date(2026, 8, 23), 850, "AMZN Refund"),
    (date(2026, 8, 27), 700, "AMZN Refund"),
]
KEYWORDS = '{"Household": ["Notebook", "Blender", "Soap", "Vase"], "Electronics": ["Cable"]}'


@pytest.fixture
def ready(ledger: Ledger, tmp_path: Path) -> Ledger:
    ledger.add_account("Checking", last4="1111")  # the exports say "Visa - 1111"
    rows = [Row(day, cents, name, f"fit-{i}") for i, (day, cents, name) in enumerate(CHARGES)]
    ledger.statement("Checking", rows)
    ledger.import_all()
    keywords = tmp_path / "amazon-keywords.json"
    keywords.write_text(KEYWORDS, encoding="utf-8")
    ledger.cli("keywords", "import", keywords, "--list", "amazon", "--apply")
    for login in ("Orders_A", "Orders_B"):
        shutil.copytree(EXPORTS / login, ledger.data_dir / "receipts" / "AMAZON" / login)
    return ledger


def amounts(ledger: Ledger) -> list[tuple[int, int]]:
    return ledger.query('SELECT id, amount_cents FROM "transaction" ORDER BY id')


def test_the_preview_changes_nothing(ready: Ledger) -> None:
    before = (ready.count("link"), ready.count("split"), ready.count("batch"))

    preview = ready.cli("receipts", "run", "amazon")

    assert "will apply: 5" in preview.output
    assert "Preview only: nothing was written" in preview.output
    assert (ready.count("link"), ready.count("split"), ready.count("batch")) == before


def test_apply_explains_charges_without_changing_money_and_undo_puts_it_back(
    ready: Ledger,
) -> None:
    total, before = ready.total_cents(), amounts(ready)
    assert total == -5725
    assert ready.count("link") == 0

    applied = ready.cli("receipts", "run", "amazon", "--apply")

    assert "Applied 5 charges as batch" in applied.output
    assert ready.count("link") == 5
    assert ready.total_cents() == total
    assert amounts(ready) == before
    # A charge with items in two categories is split, and its parts add up to the charge.
    [(_, amount, parts, sum_of_parts)] = ready.query(
        'SELECT t.id, t.amount_cents, COUNT(s.id), SUM(s.amount_cents) FROM "transaction" t '
        "JOIN split s ON s.transaction_id = t.id GROUP BY t.id"
    )
    assert (amount, parts, sum_of_parts) == (-5775, 2, -5775)
    # A charge whose items share one category takes that category whole.
    assert ready.query(
        'SELECT COUNT(*) FROM "transaction" WHERE category_id IS NOT NULL AND amount_cents < 0'
    ) == [(2,)]
    # What the receipts could not prove is left for review, unlinked and uncategorized.
    unlinked = ready.query(
        'SELECT amount_cents FROM "transaction" t WHERE NOT EXISTS (SELECT 1 FROM link l '
        "WHERE t.id IN (l.from_transaction_id, l.to_transaction_id)) ORDER BY amount_cents"
    )
    assert unlinked == [(-300,), (700,)]

    again = ready.cli("receipts", "run", "amazon", "--apply")
    assert "Nothing to apply." in again.output
    assert ready.count("link") == 5

    batch = ready.query("SELECT id FROM batch WHERE kind LIKE 'receipt%' ORDER BY id DESC")[0][0]
    ready.cli("batches", "undo", batch)

    assert ready.count("link") == 0
    assert ready.count("split") == 0
    assert ready.query('SELECT COUNT(*) FROM "transaction" WHERE category_id IS NOT NULL') == [(0,)]
    assert ready.total_cents() == total
    assert amounts(ready) == before
