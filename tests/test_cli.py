"""The serve and db commands. Keychain and PATH are faked; only tmp dirs are touched."""

import re
import sqlite3
from pathlib import Path

import pytest
import uvicorn
from alembic.config import Config
from alembic.script import ScriptDirectory
from typer.testing import CliRunner

from omsync import cli
from omsync.core.schema import MIGRATIONS_DIR

AUTO_BACKUP_NAME = re.compile(r"omsync-auto-\d{8}T\d{6}Z(-\d+)?\.db")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(data))
    monkeypatch.setenv("OMSYNC_CONFIG", str(tmp_path / "absent.toml"))
    return data


def test_serve_refuses_a_host_other_than_loopback(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMSYNC_HOST", "0.0.0.0")
    started: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(1))

    result = CliRunner().invoke(cli.app, ["serve"])

    assert result.exit_code == 1
    assert "host must be 127.0.0.1" in result.output
    assert started == []


@pytest.fixture
def db_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data directory that does not exist yet, so `db upgrade` has to create it."""
    data = tmp_path / "data"
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(data))
    monkeypatch.setenv("OMSYNC_CONFIG", str(tmp_path / "absent.toml"))
    return data


def _head() -> str:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(config).get_current_head() or ""


def _revision(db_path: Path) -> str | None:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def test_db_upgrade_backs_up_an_existing_database_first(db_home: Path) -> None:
    db_home.mkdir()
    with sqlite3.connect(db_home / "omsync.db") as connection:
        connection.execute("CREATE TABLE marker (note TEXT)")
        connection.execute("INSERT INTO marker VALUES ('written before the upgrade')")

    result = CliRunner().invoke(cli.app, ["db", "upgrade"])

    assert result.exit_code == 0, result.output
    backups = sorted((db_home / "backups").iterdir())
    assert len(backups) == 1
    assert AUTO_BACKUP_NAME.fullmatch(backups[0].name), backups[0].name
    assert f"Backed up {db_home / 'omsync.db'} to {backups[0]}" in result.output
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT note FROM marker").fetchall() == [
            ("written before the upgrade",)
        ]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "alembic_version" not in tables  # the backup is the pre-upgrade file
    assert _revision(db_home / "omsync.db") == _head()
