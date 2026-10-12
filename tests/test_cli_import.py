"""`omsync import` and `batches undo`, through the CLI in a tmp data dir.

Statement files are copies of the synthetic fixtures. The data directory is set by environment
variable, so these tests never touch ~/omsync-data."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omsync import cli

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statements"
OFX_ACCOUNT = "000012345678"  # ofx1_checking.ofx, shown as ••5678
CARD_ACCOUNT = "000098765432"  # ofx2_card.ofx, shown as ••5432
COUNT_TABLES = (
    "statement_file",
    "statement_row",
    "statement_balance",
    "transaction",
    "transaction_row",
    "import_review",
    "batch",
    "batch_change",
)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(data))
    monkeypatch.setenv("OMSYNC_CONFIG", str(tmp_path / "absent.toml"))
    return data


def run(*args: str | Path):
    return CliRunner().invoke(cli.app, [str(arg) for arg in args])


@pytest.fixture
def ready(data_dir: Path) -> Path:
    """A database at head, with a testbank checking account and a BoA checking account mapped."""
    assert run("db", "upgrade").exit_code == 0
    assert (
        run(
            "accounts",
            "add",
            "--institution",
            "testbank",
            "--institution-name",
            "Test Bank",
            "--statement-account",
            OFX_ACCOUNT,
            "--name",
            "Checking",
            "--type",
            "checking",
        ).exit_code
        == 0
    )
    assert (
        run(
            "accounts",
            "add",
            "--institution",
            "boa",
            "--institution-name",
            "Bank of America",
            "--statement-account",
            "1234",
            "--name",
            "Checking",
            "--type",
            "checking",
        ).exit_code
        == 0
    )
    return data_dir


def place(data_dir: Path, fixture: str, *folders: str, name: str | None = None) -> Path:
    target_dir = data_dir.joinpath("statements", *folders)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (name or fixture)
    shutil.copyfile(FIXTURES / fixture, target)
    return target


def place_boa_checking(data_dir: Path) -> Path:
    return place(data_dir, "boa_checking.csv", "BOA", "1234")


def place_ofx(data_dir: Path) -> Path:
    return place(data_dir, "ofx1_checking.ofx", "TESTBANK")


def counts(data_dir: Path) -> dict[str, int]:
    db = data_dir / "omsync.db"
    if not db.is_file():
        return {}
    with sqlite3.connect(db) as connection:
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in COUNT_TABLES
        }


def listing(data_dir: Path) -> list[str]:
    """Every path under the data directory, without SQLite's transient -wal and -shm files."""
    return sorted(
        str(path.relative_to(data_dir))
        for path in data_dir.rglob("*")
        if not path.name.endswith(("-wal", "-shm"))
    )


def batch_ids(data_dir: Path, kind: str) -> list[int]:
    with sqlite3.connect(data_dir / "omsync.db") as connection:
        rows = connection.execute(
            "SELECT id FROM batch WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
    return [row[0] for row in rows]


@pytest.fixture
def imported(ready: Path) -> Iterator[Path]:
    place_ofx(ready)
    result = run("import", "--apply")
    assert result.exit_code == 0, result.output
    yield ready


def test_preview_without_apply_writes_nothing(ready: Path) -> None:
    place_ofx(ready)
    place_boa_checking(ready)
    before_counts, before_files = counts(ready), listing(ready)

    result = run("import")

    assert result.exit_code == 0, result.output
    assert "ofx1_checking.ofx  account ••5678  joined 0, created 5, review 0" in result.output
    assert "boa_checking.csv  account ••1234  joined 0, created 5, review 0" in result.output
    assert "Preview only: nothing was written" in result.output
    assert counts(ready) == before_counts
    assert listing(ready) == before_files


def test_undo_restores_the_counts_from_before_the_import(ready: Path) -> None:
    before = counts(ready)
    place_ofx(ready)
    assert run("import", "--apply").exit_code == 0
    batch_id = batch_ids(ready, "import")[0]

    result = run("batches", "undo", batch_id)

    assert result.exit_code == 0, result.output
    assert f"Undid batch {batch_id} (import)." in result.output
    after = counts(ready)
    assert after["statement_file"] == before["statement_file"]
    assert after["transaction"] == before["transaction"]
    assert after["statement_row"] == before["statement_row"]
    assert after["transaction_row"] == before["transaction_row"]


def stored(data_dir: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(data_dir / "omsync.db") as connection:
        return connection.execute(sql).fetchall()
