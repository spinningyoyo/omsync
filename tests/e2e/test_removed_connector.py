"""A provider whose connector was removed from omsync after the provider was made.

The provider and what it fetched stay. The Data sync page and the account page still open, and a
sync of every provider still succeeds: the provider only takes files you add. The removal is made
by taking the connector out of its registry for the length of the test."""

from pathlib import Path

import pytest
from ledgerkit import Ledger, plain, web_client
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.cli import sync as cli_sync
from omsync.core.db import database_url, make_engine
from omsync.core.settings import Settings
from omsync.ingest.connectors import registry as bank_registry
from omsync.ledger.models import Account, Institution, Provider, ProviderAccount, ProviderKind
from omsync.receipts.connectors import registry as receipt_registry


def add_bank_provider(data_dir: Path, connector: str) -> None:
    """A bank provider at the ledger's one institution that fetches its one account."""
    engine = make_engine(database_url(Settings(data_dir=data_dir)))
    with Session(engine) as session:
        institution = session.scalars(select(Institution)).one()
        account = session.scalars(select(Account)).one()
        provider = Provider(
            key="old-bank",
            kind=ProviderKind.bank,
            name="Old bank login",
            connector_key=connector,
            institution_id=institution.id,
            vault_item_id="item-1",
        )
        session.add(provider)
        session.flush()
        session.add(ProviderAccount(provider_id=provider.id, account_id=account.id))
        session.commit()
    engine.dispose()


def remove_connector(monkeypatch: pytest.MonkeyPatch, registry, key: str) -> None:
    remaining = {k: c for k, c in registry._CONNECTORS.items() if k != key}
    monkeypatch.setattr(registry, "_CONNECTORS", remaining)


@pytest.mark.parametrize(
    ("kind", "connector", "name"),
    [
        (ProviderKind.bank, "chase", "Old bank login"),
        (ProviderKind.receipt, "costco", "Costco default"),
    ],
)
def test_a_provider_outlives_its_connector(
    ledger: Ledger,
    monkeypatch: pytest.MonkeyPatch,
    kind: ProviderKind,
    connector: str,
    name: str,
) -> None:
    ledger.add_account("Checking", last4="1234")
    if kind is ProviderKind.bank:
        add_bank_provider(ledger.data_dir, connector)
        remove_connector(monkeypatch, bank_registry, connector)
    else:
        ledger.add_receipt_provider(connector)
        remove_connector(monkeypatch, receipt_registry, connector)
    monkeypatch.setattr(cli_sync, "make_vault", lambda settings, email: None)
    client = web_client(ledger.data_dir)

    sync_page = client.get("/settings/sync", params={"tab": kind.value + "s"})
    account_page = client.get(f"/accounts/{ledger.query('SELECT id FROM account')[0][0]}")
    result = ledger.cli("sync", "--all", ok=False)

    assert sync_page.status_code == 200
    assert name in plain(sync_page.text)
    assert account_page.status_code == 200
    if kind is ProviderKind.bank:
        assert "Provider: Drop folder only" in plain(account_page.text)
    assert result.exit_code == 0, result.output
    assert ledger.count("provider") == 1
