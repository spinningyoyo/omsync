"""Statement import, re-import, duplicates, and undo, end to end through the CLI.

Statements are synthetic OFX files written into the drop folder of a data directory in tmp_path.
Money is asserted in integer cents: whatever the import joins, skips or undoes, the ledger holds
each real transaction once."""

from datetime import date, timedelta
from pathlib import Path

import pytest
from ledgerkit import Ledger, Row

DAY = date(2026, 8, 3)


def row(offset: int, cents: int, name: str, fitid: str) -> Row:
    return Row(DAY + timedelta(days=offset), cents, name, fitid)


@pytest.fixture
def checking(ledger: Ledger) -> Ledger:
    ledger.add_account("Checking", last4="1111")
    return ledger


def test_importing_the_same_statement_again_adds_nothing(checking: Ledger) -> None:
    rows = [row(0, -1250, "ACME CAFE", "a1"), row(1, 250000, "ACME PAYROLL", "a2")]
    checking.statement("Checking", rows, file="first.ofx")
    checking.import_all()
    before = (checking.total_cents(), checking.count("transaction"), checking.count("batch"))
    assert before[:2] == (248750, 2)

    # The same content under another name is the same file.
    checking.statement("Checking", rows, file="renamed.ofx")
    again = checking.import_all()

    assert (
        checking.total_cents(),
        checking.count("transaction"),
        checking.count("batch"),
    ) == before
    assert "Nothing to import." in again.output


def test_overlapping_statements_count_each_transaction_once(checking: Ledger) -> None:
    march = [row(0, -1000, "ACME CAFE", "a1"), row(1, -2000, "ACME GROCERY", "a2")]
    overlap = [*march[1:], row(2, -3000, "CITY WATER", "a3")]
    checking.statement("Checking", march, file="one.ofx")
    checking.import_all()

    checking.statement("Checking", overlap, file="two.ofx")
    result = checking.import_all()

    assert "joined 1, created 1" in result.output
    assert checking.count("transaction") == 3
    assert checking.total_cents() == -6000
    # Each statement row is kept, as evidence, and points at the one transaction.
    assert checking.count("statement_row") == 4
    assert checking.count("transaction_row") == 4


@pytest.mark.parametrize(
    ("first", "second", "expected_count", "expected_total"),
    [
        pytest.param(
            [("a1", -500), ("a2", -500)],
            [("a1", -500), ("a2", -500)],
            2,
            -1000,
            id="identical same-day purchases join one for one",
        ),
        pytest.param(
            [("a1", -500), ("a2", -500)],
            [("a1", -500), ("a2", -500), ("a3", -500)],
            3,
            -1500,
            id="a third identical purchase is new",
        ),
        pytest.param(
            [("a1", -500)],
            [("b9", -500)],
            2,
            -1000,
            id="a different bank ID is a different purchase",
        ),
    ],
)
def test_identical_purchases_are_never_merged_or_doubled(
    checking: Ledger,
    first: list[tuple[str, int]],
    second: list[tuple[str, int]],
    expected_count: int,
    expected_total: int,
) -> None:
    checking.statement("Checking", [row(0, c, "ACME CAFE", f) for f, c in first], file="one.ofx")
    checking.import_all()

    checking.statement("Checking", [row(0, c, "ACME CAFE", f) for f, c in second], file="two.ofx")
    checking.import_all()

    assert checking.count("transaction") == expected_count
    assert checking.total_cents() == expected_total


def test_the_preview_writes_nothing_and_an_unmapped_account_is_reported(checking: Ledger) -> None:
    checking.statement("Checking", [row(0, -500, "ACME CAFE", "a1")])
    stranger = checking.data_dir / "statements" / "BANK" / "stranger.ofx"
    stranger.write_text(
        (checking.data_dir / "statements" / "BANK" / "checking_1.ofx")
        .read_text(encoding="utf-8")
        .replace("00000000" + "1111", "000000009876"),
        encoding="utf-8",
    )

    preview = checking.cli("import", ok=False)

    assert preview.exit_code == 1
    assert "no account at bank is mapped to the statement account ••9876" in preview.output
    assert "Preview only: nothing was written" in preview.output
    assert checking.count("transaction") == 0
    assert checking.count("statement_file") == 0


def test_undo_returns_the_ledger_to_zero_newest_first(checking: Ledger) -> None:
    checking.add_account("Card", "credit_card", "3333")
    checking.statement("Checking", [row(0, -20000, "CREDIT CARD Bill Payment", "a1")])
    checking.statement("Card", [row(0, 20000, "PAYMENT THANK YOU", "b1")])
    checking.import_all()
    assert (checking.count("transaction"), checking.count("link")) == (2, 1)
    link_batch = checking.query("SELECT id FROM batch WHERE kind = 'reconcile.approve'")[0][0]
    imports = [r[0] for r in checking.query("SELECT id FROM batch WHERE kind = 'import'")]

    for batch in [link_batch, *sorted(imports, reverse=True)]:
        checking.cli("batches", "undo", batch)

    assert checking.count("link") == 0
    assert checking.count("transaction") == 0
    assert checking.total_cents() == 0
    assert checking.count("statement_file") == 0


def test_undo_refuses_while_a_later_link_depends_on_the_import(checking: Ledger) -> None:
    checking.add_account("Card", "credit_card", "3333")
    checking.statement("Checking", [row(0, -20000, "CREDIT CARD Bill Payment", "a1")])
    checking.statement("Card", [row(0, 20000, "PAYMENT THANK YOU", "b1")])
    checking.import_all()
    first_import = checking.query("SELECT MIN(id) FROM batch WHERE kind = 'import'")[0][0]

    refused = checking.cli("batches", "undo", first_import, ok=False)

    assert refused.exit_code == 1
    assert "referenced by link" in refused.output
    assert (checking.count("transaction"), checking.count("link")) == (2, 1)
    assert checking.total_cents() == 0


def test_undo_refuses_after_a_later_category_change_and_works_once_that_is_undone(
    checking: Ledger, tmp_path: Path
) -> None:
    checking.statement("Checking", [row(0, -1250, "ACME CAFE", "a1")])
    checking.import_all()
    import_batch = checking.query("SELECT id FROM batch WHERE kind = 'import'")[0][0]
    keywords = tmp_path / "keywords.json"
    keywords.write_text('{"Coffee": ["cafe"]}', encoding="utf-8")
    checking.cli("keywords", "import", keywords, "--list", "payee", "--apply")
    checking.cli("categorize", "--apply")
    categorize = checking.query("SELECT id FROM batch WHERE kind = 'categorize'")[0][0]

    refused = checking.cli("batches", "undo", import_batch, ok=False)

    assert refused.exit_code == 1
    assert checking.count("transaction") == 1  # nothing was lost

    checking.cli("batches", "undo", categorize)
    checking.cli("batches", "undo", import_batch)
    assert checking.count("transaction") == 0
    assert checking.total_cents() == 0
