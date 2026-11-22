"""Bank providers: how omsync fetches an institution's statements, read, and the rules for the
accounts a bank provider fetches.

A bank provider names a bank connector and the vault login it uses, and owns the accounts it
fetches. An institution may have several providers, each fetching only its own accounts into the
institution's folder. Creating, editing, and deleting a provider of any kind is in
omsync.sync.providers.
"""

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batches import (
    Change,
    next_primary_key,
    snapshot,
)
from omsync.ingest.connectors.base import ConnectorError
from omsync.ingest.connectors.registry import get_connector
from omsync.ledger.models import (
    Account,
    Institution,
    Provider,
    ProviderAccount,
)

DROP_FOLDER = "drop_folder"
KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class ProviderError(Exception):
    """A provider change that cannot be made. The message is safe to show; nothing was written."""


@dataclass(frozen=True)
class ProviderInfo:
    """A provider, with its institution and the accounts it fetches."""

    key: str
    institution_key: str
    institution_name: str
    connector_key: str
    name: str
    vault_item_id: str | None
    enabled: bool
    account_ids: tuple[int, ...]


def list_providers(session: Session) -> list[ProviderInfo]:
    """Every provider in institution key, then provider key, order. Read-only."""
    accounts: dict[int, list[int]] = {}
    for provider_id, account_id in session.execute(
        select(ProviderAccount.provider_id, ProviderAccount.account_id).order_by(
            ProviderAccount.account_id
        )
    ).all():
        accounts.setdefault(provider_id, []).append(account_id)
    rows = session.execute(
        select(Provider, Institution.key, Institution.name)
        .join(Institution, Institution.id == Provider.institution_id)
        .order_by(Institution.key, Provider.key)
    ).all()
    return [
        ProviderInfo(
            key=provider.key,
            institution_key=institution_key,
            institution_name=institution_name,
            connector_key=provider.connector_key,
            name=provider.name,
            vault_item_id=provider.vault_item_id,
            enabled=bool(provider.enabled),
            account_ids=tuple(accounts.get(provider.id, [])),
        )
        for provider, institution_key, institution_name in rows
    ]


def enabled_provider_keys(session: Session) -> list[str]:
    """The keys of the enabled providers, in the order a sync of all of them runs them."""
    return list(
        session.scalars(
            select(Provider.key)
            .join(Institution, Institution.id == Provider.institution_id)
            .where(Provider.enabled.is_(True))
            .order_by(Institution.key, Provider.key)
        ).all()
    )


def account_link_changes(
    session: Session, provider: Provider, wanted: tuple[int, ...]
) -> list[Change]:
    """Unlink the provider's accounts that are no longer wanted, then link the new ones. Unlinks
    come first, so an account is never linked twice in one batch."""
    accounts = {
        account.id: account
        for account in session.scalars(select(Account).where(Account.id.in_(wanted))).all()
    }
    for account_id in wanted:
        account = accounts.get(account_id)
        if account is None:
            raise ProviderError(f"account {account_id} does not exist")
        if account.institution_id != provider.institution_id:
            raise ProviderError(f"{account.name} is not at this provider's institution")
    taken = session.execute(
        select(Account.name, Provider.key)
        .join(ProviderAccount, ProviderAccount.account_id == Account.id)
        .join(Provider, Provider.id == ProviderAccount.provider_id)
        .where(Account.id.in_(wanted), ProviderAccount.provider_id != provider.id)
        .order_by(Account.id)
    ).first()
    if taken is not None:
        name, owner = taken
        raise ProviderError(f"{name} is already fetched by provider {owner!r}")

    linked = dict(
        session.execute(
            select(ProviderAccount.account_id, ProviderAccount.id).where(
                ProviderAccount.provider_id == provider.id
            )
        ).all()
    )
    changes = [
        Change(
            "provider_account",
            str(link_id),
            snapshot(session, "provider_account", link_id),
            None,
        )
        for account_id, link_id in linked.items()
        if account_id not in wanted
    ]
    new_ids = [account_id for account_id in wanted if account_id not in linked]
    base = next_primary_key(session, ProviderAccount.__table__)
    for offset, account_id in enumerate(new_ids):
        link_id = base + offset
        changes.append(
            Change(
                "provider_account",
                str(link_id),
                None,
                {"id": link_id, "provider_id": provider.id, "account_id": account_id},
            )
        )
    return changes


def checked_key(value: str, what: str) -> str:
    key = value.strip().lower()
    if not KEY_PATTERN.fullmatch(key):
        raise ProviderError(
            f"the {what} is 1 to 32 lowercase letters, digits, - or _, "
            "starting with a letter or digit"
        )
    return key


def bank_connector(connector_key: str) -> None:
    if connector_key == DROP_FOLDER:
        raise ProviderError("the drop folder fetches nothing, so it cannot be a provider")
    try:
        get_connector(connector_key)
    except ConnectorError:
        raise ProviderError(f"there is no connector {connector_key!r}") from None
