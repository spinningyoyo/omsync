"""Creating, editing, and deleting providers of every kind, as batches.

A provider is a bank sign-in that fetches statements for its accounts, or a receipt vendor sign-in
that fetches receipts. Both kinds share one table, one key space, and the rules here. A bank
provider also belongs to an institution and owns the accounts it fetches (see
ingest.connectors.config). Every change is one batch, so `omsync batches undo` reverses it.

Editing and deleting a provider are refused while a sync holds the sync lock. The lock is held for
the whole of the change, so no sync can start meanwhile.

The functions that take raw input, such as add_bank_provider and edit_provider, check it first and
refuse with ProviderSetupError, whose message is safe to show.
"""

import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    CHANGED_ELSEWHERE,
    BatchError,
    Change,
    StalePreview,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
    snapshot,
)
from omsync.core.locks import SYNC_LOCK_NAME, LockBusy, exclusive_lock
from omsync.core.settings import Settings
from omsync.ingest.connectors.base import ConnectorError
from omsync.ingest.connectors.config import (
    ProviderError,
    account_link_changes,
    bank_connector,
    checked_key,
)
from omsync.ingest.providers import (
    ProviderSetupError,
    account_number,
    provider_setup,
    vault_item_id,
)
from omsync.ledger.models import Institution, Provider, ProviderAccount, ProviderKind, SyncRun
from omsync.receipts import connectors as receipt_connectors
from omsync.receipts.models import ReceiptRecord

MAX_KEY = 32


def create_provider(
    session: Session,
    *,
    provider_key: str,
    provider_name: str,
    connector_key: str,
    vault_item_id: str | None,
    idempotency_key: str,
    kind: ProviderKind = ProviderKind.bank,
    institution_key: str | None = None,
    institution_name: str | None = None,
) -> Batch:
    """Add a provider, as one batch. A bank provider needs an institution: it is created in the
    same batch when it does not exist, and then institution_name is required. A receipt provider
    has none.

    Raises ProviderError, and writes nothing, when a key is malformed, the provider key is taken,
    the connector is unknown for the kind, the provider has no name, or a new institution has no
    name.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    provider_key = checked_key(provider_key, "provider key")
    _check_connector(kind, connector_key)
    if not provider_name.strip():
        raise ProviderError("the provider needs a name")
    if session.scalars(select(Provider.id).where(Provider.key == provider_key)).first() is not None:
        raise ProviderError(f"there is already a provider {provider_key!r}")

    changes: list[Change] = []
    institution_id = None
    if kind is ProviderKind.bank:
        institution_id, institution_changes = _institution(
            session, institution_key or "", institution_name
        )
        changes += institution_changes
    provider_id = next_primary_key(session, Provider.__table__)
    changes.append(
        Change(
            "provider",
            str(provider_id),
            None,
            {
                "id": provider_id,
                "key": provider_key,
                "kind": kind.value,
                "name": provider_name.strip(),
                "institution_id": institution_id,
                "connector_key": connector_key,
                "vault_item_id": vault_item_id,
                "settings": {},
                "enabled": True,
            },
        )
    )
    summary = {"provider": provider_key}
    if institution_key:
        summary["institution"] = institution_key
    preview = make_preview(session, "provider_create", changes, summary=summary)
    return apply(session, preview, idempotency_key=idempotency_key)


def update_provider(
    session: Session,
    settings: Settings,
    *,
    provider_key: str,
    vault_item_id: str | None,
    idempotency_key: str,
    name: str | None = None,
    account_ids: Iterable[int] | None = None,
) -> Batch | None:
    """Set a provider's vault login, and its name when given, as one batch. For a bank provider,
    account_ids, when given, replaces the accounts it fetches. Returns None when nothing would
    change.

    Raises ProviderError, and writes nothing, when the provider is unknown, the name is empty, an
    account is not eligible (see account_link_changes), a receipt provider is given accounts, or a
    sync is running.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    wanted = None if account_ids is None else tuple(dict.fromkeys(account_ids))
    with not_syncing(settings):
        provider = _provider(session, provider_key)
        if name is not None and not name.strip():
            raise ProviderError("the provider needs a name")
        changes = _provider_change(session, provider, vault_item_id, name)
        if wanted is not None:
            if provider.kind is not ProviderKind.bank and wanted:
                raise ProviderError("a receipt provider fetches no accounts")
            if provider.kind is ProviderKind.bank:
                changes += account_link_changes(session, provider, wanted)
        if not changes:
            return None
        summary: dict[str, object] = {"provider": provider_key}
        if wanted is not None and provider.kind is ProviderKind.bank:
            summary["accounts"] = len(wanted)
        preview = make_preview(session, "provider_update", changes, summary=summary)
        return apply(session, preview, idempotency_key=idempotency_key)


def delete_provider(
    session: Session, settings: Settings, *, provider_key: str, idempotency_key: str
) -> Batch:
    """Delete a provider, the links to its accounts, its sync runs, and its receipt rows, as one
    batch. Accounts, transactions, and the statement and receipt files stay. Sync runs and receipt
    rows are read back from the files or the provider, so they are deleted in the batch's
    transaction and undo does not restore them.

    Raises ProviderError, and writes nothing, when the provider is unknown or a sync is running.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    with not_syncing(settings):
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
        session.execute(delete(SyncRun).where(SyncRun.provider_id == provider.id))
        for record in session.scalars(
            select(ReceiptRecord).where(ReceiptRecord.provider_id == provider.id)
        ):
            session.delete(record)
        session.flush()
        return apply(session, preview, idempotency_key=idempotency_key)


def new_provider_key(session: Session, name: str, fallback: str) -> str:
    """A key for a new provider: the name as lowercase words joined by hyphens, or fallback when
    the name has none. A taken key gets -2, -3, and so on."""
    words = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    base = (words or fallback)[: MAX_KEY - 3].strip("-") or fallback
    taken = set(session.scalars(select(Provider.key).where(Provider.key.like(f"{base}%"))))
    key, number = base, 1
    while key in taken:
        number += 1
        key = f"{base}-{number}"
    return key


@contextmanager
def not_syncing(settings: Settings) -> Iterator[None]:
    """Hold the sync lock for a change to a provider, so no sync starts while it is made. Nothing
    waits: a held lock refuses the change at once."""
    try:
        with exclusive_lock(settings.data_dir / SYNC_LOCK_NAME):
            yield
    except LockBusy:
        raise ProviderError("a sync is running; try again when it finishes") from None


# From raw input


def add_bank_provider(
    session: Session,
    *,
    institution: str,
    name: str,
    provider: str,
    connector: str,
    vault_login: str | None,
    has_vault: bool,
    idempotency_key: str,
) -> Batch:
    """Add a bank provider with its vault login, creating the institution first when it is new, as
    one batch. Without a vault there is no login, because you sign in at the bank. A double submit
    returns the batch the first one made."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    setup = provider_setup(
        session, institution=institution, name=name, provider=provider, connector=connector
    )
    if has_vault and not vault_login:
        raise ProviderSetupError("Choose a login from the search results.")
    login = vault_item_id(vault_login) if has_vault and vault_login else None
    return _setup_write(
        lambda: create_provider(
            session,
            institution_key=setup.institution_key,
            institution_name=setup.institution_name,
            provider_key=setup.provider_key,
            provider_name=setup.provider_name,
            connector_key=setup.connector_key,
            vault_item_id=login,
            idempotency_key=idempotency_key,
        )
    )


def edit_provider(
    session: Session,
    settings: Settings,
    *,
    provider_key: str,
    login_id: str | None,
    account_ids: Sequence[str],
    idempotency_key: str,
    name: str | None = None,
) -> Batch | None:
    """Set a provider's name (when given) and its vault login (the current one when login_id is
    None), and for a bank provider the accounts it fetches, as one batch. `account_ids` are account
    numbers as text. None means nothing would change."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = session.scalars(select(Provider).where(Provider.key == provider_key)).first()
    if current is None:
        raise ProviderSetupError(CHANGED_ELSEWHERE, conflict=True)
    login = current.vault_item_id if login_id is None else vault_item_id(login_id)
    ids = (
        [account_number(value) for value in account_ids]
        if current.kind is ProviderKind.bank
        else None
    )
    return _setup_write(
        lambda: update_provider(
            session,
            settings,
            provider_key=provider_key,
            vault_item_id=login,
            account_ids=ids,
            idempotency_key=idempotency_key,
            name=name,
        )
    )


def remove_provider(
    session: Session, settings: Settings, *, provider_key: str, idempotency_key: str
) -> Batch:
    """Delete a provider as one batch. Its accounts, statements, and receipt files stay."""
    return _setup_write(
        lambda: delete_provider(
            session, settings, provider_key=provider_key, idempotency_key=idempotency_key
        )
    )


def _setup_write[T](write: Callable[[], T]) -> T:
    try:
        return write()
    except ProviderError as exc:
        raise ProviderSetupError(str(exc)) from None
    except StalePreview as exc:
        raise ProviderSetupError(CHANGED_ELSEWHERE, conflict=True) from exc
    except BatchError as exc:
        raise ProviderSetupError(str(exc), conflict=True) from exc


def _check_connector(kind: ProviderKind, connector_key: str) -> None:
    if kind is ProviderKind.bank:
        bank_connector(connector_key)
        return
    try:
        receipt_connectors.get_connector(connector_key)
    except ConnectorError:
        raise ProviderError(f"there is no receipt vendor {connector_key!r}") from None


def _institution(
    session: Session, institution_key: str, institution_name: str | None
) -> tuple[int, list[Change]]:
    """The institution's ID, and the change that creates it when it does not exist yet."""
    institution = session.scalars(
        select(Institution).where(Institution.key == institution_key)
    ).first()
    if institution is not None:
        return institution.id, []
    key = checked_key(institution_key, "institution key")
    if not institution_name:
        raise ProviderError(f"there is no institution {key!r} yet; give its name")
    institution_id = next_primary_key(session, Institution.__table__)
    row = {"id": institution_id, "key": key, "name": institution_name}
    return institution_id, [Change("institution", str(institution_id), None, row)]


def _provider_change(
    session: Session, provider: Provider, vault_item_id: str | None, name: str | None
) -> list[Change]:
    before = snapshot(session, "provider", provider.id)
    if before is None:
        return []
    after = {**before, "vault_item_id": vault_item_id}
    if name is not None:
        after["name"] = name.strip()
    return [] if after == before else [Change("provider", str(provider.id), before, after)]


def _provider(session: Session, provider_key: str) -> Provider:
    provider = session.scalars(select(Provider).where(Provider.key == provider_key)).first()
    if provider is None:
        raise ProviderError(f"there is no provider {provider_key!r}")
    return provider
