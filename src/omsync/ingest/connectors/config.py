"""Providers: how omsync fetches an institution's statements, and changing them as batches.

A provider names a bank connector and the vault login it uses, and owns the accounts it fetches. An
institution may have several providers, each fetching only its own accounts into the institution's
folder. Every change is one batch, so `omsync batches undo` reverses it.

Editing a provider's login or accounts, and deleting it, are refused while a sync holds the sync
lock. The lock is held for the whole of the change, so no sync of any provider can start meanwhile.
"""

import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
    snapshot,
)
from omsync.core.locks import SYNC_LOCK_NAME, LockBusy, exclusive_lock
from omsync.core.settings import Settings
from omsync.ingest.connectors.base import ConnectorError
from omsync.ingest.connectors.registry import get_connector
from omsync.ledger.models import Account, Institution, Provider, ProviderAccount

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


def create_provider(
    session: Session,
    *,
    institution_key: str,
    institution_name: str | None,
    provider_key: str,
    connector_key: str,
    vault_item_id: str | None,
    idempotency_key: str,
) -> Batch:
    """Add a provider for an institution, as one batch. The institution is created in the same batch
    when it does not exist, and then institution_name is required. It is ignored otherwise.

    Raises ProviderError, and writes nothing, when a key is malformed, the provider key is taken,
    the connector is unknown or is the drop folder, or a new institution has no name.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    provider_key = _checked_key(provider_key, "provider key")
    _bank_connector(connector_key)
    if session.scalars(select(Provider.id).where(Provider.key == provider_key)).first() is not None:
        raise ProviderError(f"there is already a provider {provider_key!r}")

    changes: list[Change] = []
    institution = session.scalars(
        select(Institution).where(Institution.key == institution_key)
    ).first()
    if institution is None:
        institution_key = _checked_key(institution_key, "institution key")
        if not institution_name:
            raise ProviderError(f"there is no institution {institution_key!r} yet; give its name")
        institution_id = next_primary_key(session, Institution.__table__)
        changes.append(
            Change(
                "institution",
                str(institution_id),
                None,
                {"id": institution_id, "key": institution_key, "name": institution_name},
            )
        )
    else:
        institution_id = institution.id

    provider_id = next_primary_key(session, Provider.__table__)
    changes.append(
        Change(
            "provider",
            str(provider_id),
            None,
            {
                "id": provider_id,
                "key": provider_key,
                "institution_id": institution_id,
                "connector_key": connector_key,
                "vault_item_id": vault_item_id,
                "settings": {},
                "enabled": True,
            },
        )
    )
    preview = make_preview(
        session,
        "provider_create",
        changes,
        summary={"institution": institution_key, "provider": provider_key},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def update_provider(
    session: Session,
    settings: Settings,
    *,
    provider_key: str,
    vault_item_id: str | None,
    account_ids: Iterable[int],
    idempotency_key: str,
) -> Batch | None:
    """Set a provider's vault login and replace the accounts it fetches, as one batch. Returns None
    when nothing would change.

    Every account must be at the provider's institution and fetched by no other provider. Raises
    ProviderError, and writes nothing, when the provider is unknown, an account is not eligible, or
    a
    sync of it is running.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    wanted = tuple(dict.fromkeys(account_ids))
    with _not_syncing(settings, provider_key):
        provider = _provider(session, provider_key)
        changes = _login_change(session, provider, vault_item_id)
        changes += _account_changes(session, provider, wanted)
        if not changes:
            return None
        preview = make_preview(
            session,
            "provider_update",
            changes,
            summary={"provider": provider_key, "accounts": len(wanted)},
        )
        return apply(session, preview, idempotency_key=idempotency_key)


def delete_provider(
    session: Session, settings: Settings, *, provider_key: str, idempotency_key: str
) -> Batch:
    """Delete a provider and the links to its accounts, as one batch. Its accounts, transactions,
    statement files, and sync history stay.

    Raises ProviderError, and writes nothing, when the provider is unknown or a sync of it is
    running.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    with _not_syncing(settings, provider_key):
        provider = _provider(session, provider_key)
        links = session.scalars(
            select(ProviderAccount.id).where(ProviderAccount.provider_id == provider.id)
        ).all()
        changes = [
            Change(
                "provider_account",
                str(link_id),
                snapshot(session, "provider_account", link_id),
                None,
            )
            for link_id in links
        ]
        changes.append(
            Change("provider", str(provider.id), snapshot(session, "provider", provider.id), None)
        )
        preview = make_preview(
            session,
            "provider_delete",
            changes,
            summary={"provider": provider_key, "accounts": len(links)},
        )
        return apply(session, preview, idempotency_key=idempotency_key)


def _login_change(session: Session, provider: Provider, vault_item_id: str | None) -> list[Change]:
    before = snapshot(session, "provider", provider.id)
    if before is None or before["vault_item_id"] == vault_item_id:
        return []
    after = {**before, "vault_item_id": vault_item_id}
    return [Change("provider", str(provider.id), before, after)]


def _account_changes(session: Session, provider: Provider, wanted: tuple[int, ...]) -> list[Change]:
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


def _provider(session: Session, provider_key: str) -> Provider:
    provider = session.scalars(select(Provider).where(Provider.key == provider_key)).first()
    if provider is None:
        raise ProviderError(f"there is no provider {provider_key!r}")
    return provider


def _checked_key(value: str, what: str) -> str:
    key = value.strip().lower()
    if not KEY_PATTERN.fullmatch(key):
        raise ProviderError(
            f"the {what} is 1 to 32 lowercase letters, digits, - or _, "
            "starting with a letter or digit"
        )
    return key


def _bank_connector(connector_key: str) -> None:
    if connector_key == DROP_FOLDER:
        raise ProviderError("the drop folder fetches nothing, so it cannot be a provider")
    try:
        get_connector(connector_key)
    except ConnectorError:
        raise ProviderError(f"there is no connector {connector_key!r}") from None


@contextmanager
def _not_syncing(settings: Settings, provider_key: str) -> Iterator[None]:
    """Hold the sync lock for a change, so no sync starts while it is made. Nothing waits: a held
    lock refuses the change at once."""
    try:
        with exclusive_lock(settings.data_dir / SYNC_LOCK_NAME):
            yield
    except LockBusy:
        raise ProviderError(
            f"provider {provider_key!r} is syncing, or a receipt download is running; "
            "try again when it finishes"
        ) from None
