"""`omsync sync`, through the CLI in a tmp data dir.

The vault is a fake that accepts one master password, and the connector is a fake that copies
synthetic fixtures. Nothing runs bw, reads the keychain, or touches ~/omsync-data. The password
must never appear in output. Providers are made with the domain functions, because the web page
that edits them is not part of the CLI."""

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from omsync import cli
from omsync.cli import sync as cli_sync
from omsync.cli.common import ready_session
from omsync.core.vault import VaultError
from omsync.ingest.connectors import registry
from omsync.ingest.connectors.config import create_provider, update_provider
from omsync.ledger.accounts import find_account

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "statements"
PASSWORD = "correct-horse-battery"
OFX_ACCOUNT = "000012345678"
CARD_ACCOUNT = "000098765432"


class FakeVault:
    """Accepts one master password, and counts unlocks and locks."""

    def __init__(self) -> None:
        self.unlock_calls = 0
        self.lock_calls = 0

    def unlock(self, password: str) -> None:
        self.unlock_calls += 1
        if password != PASSWORD:
            raise VaultError("Incorrect master password")

    def lock(self) -> None:
        self.lock_calls += 1


@dataclass
class FakeConnector:
    """Records each fetch: its login, its accounts' last four digits, and its folder. errors maps a
    vault login to the exception its fetch raises."""

    key: str = "fake"
    label: str = "Fake connector for tests"
    errors: dict[str, Exception] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)

    def fetch(self, accounts, period, vault, prompt, dest, *, vault_item_id):
        self.calls.append(
            {
                "vault_item_id": vault_item_id,
                "last4": [account.last4 for account in accounts],
                "dest": dest,
            }
        )
        if vault_item_id in self.errors:
            raise self.errors[vault_item_id]
        dest.mkdir(parents=True, exist_ok=True)
        written = []
        for name in ("ofx1_checking.ofx", "ofx2_card.ofx"):
            target = dest / name
            shutil.copyfile(FIXTURES / name, target)
            written.append(target)
        return written


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(data))
    monkeypatch.setenv("OMSYNC_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("OMSYNC_EMAIL", "ana@example.test")
    return data


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> FakeVault:
    fake = FakeVault()
    monkeypatch.setattr(cli_sync, "make_vault", lambda settings, email: fake)
    return fake


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeConnector:
    connector = FakeConnector()
    monkeypatch.setattr(registry, "_CONNECTORS", {**registry._CONNECTORS, connector.key: connector})
    return connector


@pytest.fixture
def ready(data_dir: Path) -> Path:
    """A database at head with a testbank checking account and a card account."""
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
            "testbank",
            "--institution-name",
            "Test Bank",
            "--statement-account",
            CARD_ACCOUNT,
            "--name",
            "Card",
            "--type",
            "credit_card",
        ).exit_code
        == 0
    )
    return data_dir


def add_provider(key: str, login: str, *statement_account_ids: str) -> None:
    """A provider at testbank with a fake connector, fetching the accounts those IDs name."""
    with ready_session() as (settings, session):
        create_provider(
            session,
            institution_key="testbank",
            institution_name=None,
            provider_key=key,
            connector_key="fake",
            vault_item_id=login,
            idempotency_key=f"create-{key}",
        )
        account_ids = [find_account(session, "testbank", sid).id for sid in statement_account_ids]
        update_provider(
            session,
            settings,
            provider_key=key,
            vault_item_id=login,
            account_ids=account_ids,
            idempotency_key=f"accounts-{key}",
        )


@pytest.fixture
def connected(ready: Path, fake: FakeConnector) -> Path:
    """One provider, testbank, with both accounts and login item-1."""
    add_provider("testbank", "item-1", OFX_ACCOUNT, CARD_ACCOUNT)
    return ready


def run(*args: str, input: str | None = None):
    return CliRunner().invoke(cli.app, [str(arg) for arg in args], input=input)


def sync(*args: str, password: str = PASSWORD):
    return run("sync", *args, input=f"{password}\n")


def counts(data_dir: Path) -> tuple[int, int]:
    with sqlite3.connect(data_dir / "omsync.db") as connection:
        files = connection.execute("SELECT COUNT(*) FROM statement_file").fetchone()[0]
        transactions = connection.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0]
    return files, transactions


def test_sync_asks_for_the_password_then_fetches_and_imports(
    connected: Path, vault: FakeVault, fake: FakeConnector
) -> None:
    result = sync("testbank")

    assert result.exit_code == 0, result.output
    assert "Fetched 2 files for testbank" in result.output
    assert "ofx1_checking.ofx  account ••5678  joined 0, created 5, review 0" in result.output
    assert "ofx2_card.ofx  account ••5432  joined 0, created 4, review 0" in result.output
    assert PASSWORD not in result.output
    assert vault.unlock_calls == 1
    assert vault.lock_calls == 1
    assert fake.calls[0]["vault_item_id"] == "item-1"
    assert fake.calls[0]["last4"] == ["5678", "5432"]
    assert counts(connected) == (2, 9)
