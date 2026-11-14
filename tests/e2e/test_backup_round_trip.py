"""Backup and restore of a real imported ledger, end to end through the CLI.

The existing CLI backup tests cover the swap itself. These drive the whole journey a person
follows: import, back up, keep working, restore, and check every cent and statement file is as it
was at the backup."""

import io
import re
import tarfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from ledgerkit import Ledger, Row

DAY = date(2026, 8, 3)
ARCHIVE_LINE = re.compile(r"Backed up \d+ files to (\S+) \(")


def row(offset: int, cents: int, name: str, fitid: str) -> Row:
    return Row(DAY + timedelta(days=offset), cents, name, fitid)


@pytest.fixture
def worked(ledger: Ledger) -> tuple[Ledger, Path, dict]:
    """An imported ledger with a link, and an archive of it. Returns the state at the backup."""
    ledger.add_account("Checking", last4="1111")
    ledger.add_account("Card", "credit_card", "3333")
    ledger.statement("Checking", [row(0, -20000, "CREDIT CARD Bill Payment", "a1")], file="c.ofx")
    ledger.statement("Card", [row(0, 20000, "PAYMENT THANK YOU", "b1")], file="k.ofx")
    ledger.import_all()
    created = ledger.cli("backup", "create")
    match = ARCHIVE_LINE.search(created.output)
    assert match, created.output
    state = {
        "total": ledger.total_cents(),
        "transactions": ledger.count("transaction"),
        "links": ledger.count("link"),
        "files": {
            p.name: p.read_bytes() for p in sorted((ledger.data_dir / "statements").rglob("*.ofx"))
        },
    }
    return ledger, Path(match.group(1)), state


def test_restore_returns_every_cent_and_statement_file_to_the_backup(worked) -> None:
    ledger, archive, before = worked
    # More work after the backup: a new statement and a new purchase.
    ledger.statement("Checking", [row(3, -1250, "ACME CAFE", "a9")], file="later.ofx")
    ledger.import_all()
    (ledger.data_dir / "statements" / "BANK" / "c.ofx").write_bytes(b"damaged")
    assert ledger.total_cents() == before["total"] - 1250

    restored = ledger.cli("backup", "restore", archive, "--yes")

    assert "Saved the state from before the restore to" in restored.output
    assert ledger.total_cents() == before["total"]
    assert ledger.count("transaction") == before["transactions"]
    assert ledger.count("link") == before["links"]
    after = {
        p.name: p.read_bytes() for p in sorted((ledger.data_dir / "statements").rglob("*.ofx"))
    }
    assert after == before["files"]  # the damaged file is back and the later one is gone
    # The restored ledger works: a fresh import of the later statement adds its purchase once.
    ledger.statement("Checking", [row(3, -1250, "ACME CAFE", "a9")], file="later2.ofx")
    ledger.import_all()
    assert ledger.total_cents() == before["total"] - 1250


def test_restore_without_confirmation_says_what_it_would_replace_and_changes_nothing(
    worked,
) -> None:
    ledger, archive, _before = worked
    ledger.statement("Checking", [row(3, -1250, "ACME CAFE", "a9")], file="later.ofx")
    ledger.import_all()
    total = ledger.total_cents()

    result = ledger.cli("backup", "restore", archive)

    assert result.exit_code == 0
    assert "Nothing was changed. Run again with --yes to restore." in result.output
    assert "replace the current database, statements and receipts" in result.output
    assert ledger.total_cents() == total
    assert ledger.total_cents() == total


def test_a_tampered_archive_is_refused_before_anything_is_replaced(worked, tmp_path: Path) -> None:
    ledger, archive, _before = worked
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            if member.name.endswith("c.ofx"):
                member.size = len(b"tampered")
                target.addfile(member, io.BytesIO(b"tampered"))
            else:
                target.addfile(member, source.extractfile(member))
    ledger.statement("Checking", [row(3, -1250, "ACME CAFE", "a9")], file="later.ofx")
    ledger.import_all()
    total = ledger.total_cents()

    result = ledger.cli("backup", "restore", tampered, "--yes", ok=False)

    assert result.exit_code == 1
    assert "does not match its checksum" in result.output
    assert ledger.total_cents() == total
