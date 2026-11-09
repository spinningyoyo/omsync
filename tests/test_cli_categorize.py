"""`omsync categorize` preview, apply, and undo, through the CLI in a tmp data dir.

The statement is a copy of the synthetic OFX fixture: ACME CAFE, ACME GROCERY, and three
payees with no keyword. The data directory is set by environment variable, so these tests never
touch ~/omsync-data."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omsync import cli

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statements"
OFX_ACCOUNT = "000012345678"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(data))
    monkeypatch.setenv("OMSYNC_CONFIG", str(tmp_path / "absent.toml"))
    return data


def run(*args: str | Path):
    return CliRunner().invoke(cli.app, [str(arg) for arg in args])


def scalar(data_dir: Path, sql: str, *params: object) -> int:
    with sqlite3.connect(data_dir / "omsync.db") as connection:
        return connection.execute(sql, params).fetchone()[0]


@pytest.fixture
def ready(data_dir: Path) -> Iterator[Path]:
    """A database at head with five transactions imported from the synthetic OFX file."""
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
    target = data_dir / "statements" / "TESTBANK"
    target.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "ofx1_checking.ofx", target / "ofx1_checking.ofx")
    assert run("import", "--apply").exit_code == 0
    yield data_dir


def keyword_file(tmp_path: Path, text: str, name: str = "keywords.json") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_categorize_previews_then_applies_and_undo_reverses_it(ready: Path, tmp_path: Path) -> None:
    file = keyword_file(tmp_path, '{"Coffee": ["cafe"], "Groceries": ["grocery"]}')
    assert run("keywords", "import", file, "--list", "payee", "--apply").exit_code == 0

    preview = run("categorize")

    assert preview.exit_code == 0, preview.output
    assert "Considered 5 transactions; 2 would change." in preview.output
    assert "(unset) → Coffee: 1" in preview.output
    assert "(unset) → Groceries: 1" in preview.output
    assert "Preview only: nothing was written" in preview.output
    assert scalar(ready, 'SELECT COUNT(*) FROM "transaction" WHERE category_id IS NOT NULL') == 0

    applied = run("categorize", "--apply")

    assert applied.exit_code == 0, applied.output
    assert "Categorized 2 transactions as batch" in applied.output
    assert (
        scalar(ready, "SELECT COUNT(*) FROM \"transaction\" WHERE category_origin = 'keyword'") == 2
    )
    assert scalar(ready, "SELECT COUNT(*) FROM batch WHERE kind = 'categorize'") == 1
    batch_id = scalar(ready, "SELECT id FROM batch WHERE kind = 'categorize'")

    again = run("categorize", "--apply")
    assert "Nothing to categorize." in again.output
    assert scalar(ready, "SELECT COUNT(*) FROM batch WHERE kind = 'categorize'") == 1

    undone = run("batches", "undo", batch_id)
    assert undone.exit_code == 0, undone.output
    assert scalar(ready, 'SELECT COUNT(*) FROM "transaction" WHERE category_id IS NOT NULL') == 0
