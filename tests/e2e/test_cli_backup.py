"""`omsync backup create` and `restore`, through the CLI in a tmp data dir.

The restore drill migrates a database, adds rows, a statement file and a receipt, archives them,
then damages all three and restores. A swap that fails part way must put the previous data back.
Everything lives under tmp_path; nothing touches ~/omsync-data or the keychain."""

import os
import re
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omsync import cli

STATEMENT = b"DATE,AMOUNT,PAYEE\n2026-03-01,-12.50,EXAMPLE COFFEE\n"
RECEIPT = b'{"transactionBarcode": "0001", "total": 1250}'
STATEMENT_PATH = Path("statements/EXAMPLE/example_1111.csv")
RECEIPT_PATH = Path("receipts/COSTCO/2026/Q1/0001.json")
ARCHIVE_LINE = re.compile(r"Backed up \d+ files to (\S+) \(")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The data directory, with a config file next to it. The config holds no secrets."""
    data = tmp_path / "data"
    config = tmp_path / "config" / "omsync.toml"
    config.parent.mkdir()
    config.write_text('email = "ana@example.test"\n', encoding="utf-8")
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(data))
    monkeypatch.setenv("OMSYNC_CONFIG", str(config))
    return data


def run(*args: str):
    return CliRunner().invoke(cli.app, [str(arg) for arg in args])


def execute(data: Path, sql: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(data / "omsync.db")) as connection, connection:
        connection.execute(sql, params)


def institution_keys(data: Path) -> list[str]:
    with closing(sqlite3.connect(data / "omsync.db")) as connection:
        rows = connection.execute("SELECT key FROM institution ORDER BY key").fetchall()
    return [row[0] for row in rows]


def seed(data: Path) -> None:
    """A migrated database with three institutions, a statement file and a receipt."""
    assert run("db", "upgrade").exit_code == 0
    for key, name in [
        ("example-bank", "Example Bank"),
        ("other-bank", "Other Bank"),
        ("third-bank", "Third Bank"),
    ]:
        execute(data, "INSERT INTO institution (key, name) VALUES (?, ?)", (key, name))
    (data / STATEMENT_PATH).parent.mkdir(parents=True)
    (data / STATEMENT_PATH).write_bytes(STATEMENT)
    (data / RECEIPT_PATH).parent.mkdir(parents=True)
    (data / RECEIPT_PATH).write_bytes(RECEIPT)


@pytest.fixture
def archived(home: Path) -> Path:
    """A seeded data directory and an archive of it. Returns the archive's path."""
    seed(home)
    created = run("backup", "create")
    assert created.exit_code == 0, created.output
    match = ARCHIVE_LINE.search(created.output)
    assert match is not None, created.output
    return Path(match.group(1))


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under root, except backups/ and lock files: a run may add those."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.name.endswith(".lock")
        and path.relative_to(root).parts[0] != "backups"
    }


def _members(archive: Path) -> dict[str, bytes]:
    with tarfile.open(archive, "r:gz") as tar:
        return {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
        }


def test_restore_drill_brings_back_rows_statement_and_receipt(home: Path, archived: Path) -> None:
    config = home.parent / "config" / "omsync.toml"
    original_config = config.read_bytes()
    # Damage: a row is deleted, the receipt is deleted, and the statement is edited.
    execute(home, "DELETE FROM institution WHERE key = ?", ("other-bank",))
    (home / RECEIPT_PATH).unlink()
    (home / STATEMENT_PATH).write_bytes(b"edited after the backup")
    config.unlink()  # as on a new Mac, where the config is not there yet

    result = run("backup", "restore", archived, "--yes")

    assert result.exit_code == 0, result.output
    assert institution_keys(home) == ["example-bank", "other-bank", "third-bank"]
    assert (home / STATEMENT_PATH).read_bytes() == STATEMENT
    assert (home / RECEIPT_PATH).read_bytes() == RECEIPT
    assert config.read_bytes() == original_config
    assert "Wrote omsync.toml from the archive" in result.output
    # The state from before the restore is kept as a pre-restore archive, and holds the damage.
    (safety,) = [p for p in (home / "backups").iterdir() if p.name.endswith("-pre-restore.tar.gz")]
    assert "Saved the state from before the restore to" in result.output
    saved = _members(safety)
    assert saved["data/statements/EXAMPLE/example_1111.csv"] == b"edited after the backup"
    assert "data/receipts/COSTCO/2026/Q1/0001.json" not in saved
    listing = run("backup", "list").output
    assert "manual" in listing and "pre-restore" in listing


def test_a_swap_that_fails_part_way_puts_the_previous_data_back(
    home: Path, archived: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute(home, "DELETE FROM institution WHERE key = ?", ("other-bank",))
    (home / STATEMENT_PATH).write_bytes(b"edited after the backup")
    before = _tree(home)
    real_rename = os.rename

    def failing_rename(source, destination):
        # The third move, of statements/ from the archive into the data directory, fails.
        if Path(source).name == "statements" and Path(source).parent.name == "new":
            raise OSError("simulated disk error")
        return real_rename(source, destination)

    monkeypatch.setattr(os, "rename", failing_rename)

    result = run("backup", "restore", archived, "--yes")

    assert result.exit_code == 1, result.output
    assert "the previous data is back in place" in result.output
    assert _tree(home) == before
    assert institution_keys(home) == ["example-bank", "third-bank"]
