"""Setting up providers and their accounts, from raw input: the checks, then the batches.

This is what the add-provider wizard, the Bank sync page, and scripts/setup_institutions.py share.
Their inputs are text as a person typed it, so each function checks it first and refuses with
ProviderSetupError, whose message is safe to show. Nothing is written when one is raised.

Each write is one batch from omsync.core.batches under the caller's idempotency key, so a repeat
applies once. Adding, editing, and deleting a provider are batches from connectors.config.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError, StalePreview, applied_batch_for_key
from omsync.core.settings import Settings
from omsync.ingest.connectors.base import ConnectorError
from omsync.ingest.connectors.config import (
    DROP_FOLDER,
    KEY_PATTERN,
    ProviderError,
    create_provider,
    delete_provider,
    update_provider,
)
from omsync.ingest.connectors.registry import get_connector, registered_connectors
from omsync.ingest.discovery import mask_account_numbers
from omsync.ledger.accounts import (
    AccountError,
    NewAccount,
    add_alias,
    create_account,
    create_accounts_without_alias,
    last4_of,
)
from omsync.ledger.models import AccountType, Institution, Provider

VAULT_ITEM_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
NAME_MAX = 60
STALE = "This changed elsewhere, reload."


class ProviderSetupError(Exception):
    """A setup action that cannot be done. The message is safe to show; nothing was written.
    `conflict` is set when the cause is that something changed since the person last looked."""

    def __init__(self, message: str, *, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict


# Validation


def institution_key(value: str) -> str:
    return _key_value(value, "institution key")


def display_name(value: str, what: str = "name") -> str:
    name = value.strip()
    if not name or len(name) > NAME_MAX:
        raise ProviderSetupError(f"Give the {what} as 1 to {NAME_MAX} characters.")
    return name


def connector_key(value: str) -> str:
    try:
        get_connector(value)
    except ConnectorError:
        raise ProviderSetupError("Choose a bank from the list.") from None
    if value == DROP_FOLDER:
        raise ProviderSetupError("Choose a bank from the list.")
    return value


def vault_item_id(value: str) -> str:
    if not VAULT_ITEM_ID.fullmatch(value):
        raise ProviderSetupError("Choose a login from the search results.")
    return value


def account_number(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ProviderSetupError("Choose accounts from the list.") from None


def connector_choices() -> list[tuple[str, str]]:
    """(key, label) for every bank connector. The drop folder is not offered."""
    connectors = [c for c in registered_connectors() if c.key != DROP_FOLDER]
    return [(connector.key, connector.label) for connector in connectors]


def connector_label(key: str) -> str:
    try:
        return get_connector(key).label
    except ConnectorError:
        return key


def last4_is_statement_id(connector: str) -> bool:
    """True when the connector names its accounts by last four digits (BoA)."""
    try:
        return get_connector(connector).last4_is_statement_id
    except ConnectorError:
        return False


def institution_exists(session: Session, key: str) -> bool:
    return session.scalars(select(Institution.id).where(Institution.key == key)).first() is not None


def provider_exists(session: Session, key: str) -> bool:
    return session.scalars(select(Provider.id).where(Provider.key == key)).first() is not None


# Providers


@dataclass(frozen=True)
class ProviderSetup:
    """What the wizard's first step saves: an institution (new or existing), a provider for it, and
    the provider's bank. The names are normalized."""

    institution_key: str
    institution_name: str
    provider_name: str
    provider_key: str
    connector_key: str


def provider_setup(
    session: Session, *, institution: str, name: str, provider: str, connector: str
) -> ProviderSetup:
    """Check the wizard's first step. The name typed is the provider's label, and also the name of
    a new institution; an existing institution keeps its own name. The provider key defaults to the
    institution key and must not be taken."""
    key = institution_key(institution)
    provider_value = _key_value(provider.strip() or key, "provider key")
    bank = connector_key(connector)
    if provider_exists(session, provider_value):
        raise ProviderSetupError(
            f"There is already a provider with the key {provider_value}. Choose another."
        )
    existing = session.scalars(select(Institution).where(Institution.key == key)).first()
    label = display_name(name, "display name")
    display = existing.name if existing is not None else label
    return ProviderSetup(key, display, label, provider_value, bank)


def add_provider(
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
    """Add a provider with its vault login, creating the institution first when it is new, as one
    batch. Without a vault there is no login, because you sign in at the bank. A double submit
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
    return _provider_write(
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
    """Set a provider's name (when given), its vault login (the current one when login_id is
    None) and the accounts it fetches, as one batch. `account_ids` are account numbers as text.
    None means nothing would change."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = session.scalars(select(Provider).where(Provider.key == provider_key)).first()
    if current is None:
        raise ProviderSetupError(STALE, conflict=True)
    login = current.vault_item_id if login_id is None else vault_item_id(login_id)
    ids = [account_number(value) for value in account_ids]
    return _provider_write(
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
    """Delete a provider as one batch. Its accounts and statements stay."""
    return _provider_write(
        lambda: delete_provider(
            session, settings, provider_key=provider_key, idempotency_key=idempotency_key
        )
    )


# Accounts


def map_new_account(
    session: Session,
    *,
    institution: str,
    statement_account_id: str,
    name: str,
    account_type: str,
    joint: bool,
    idempotency_key: str,
) -> Batch:
    """Create an account for a statement account, as one batch."""
    row = session.scalars(select(Institution).where(Institution.key == institution)).first()
    if row is None:
        raise ProviderSetupError("The institution does not exist yet. Start the wizard again.")
    name = display_name(name, "account name")
    try:
        type_value = AccountType(account_type).value
    except ValueError:
        raise ProviderSetupError("Choose an account type from the list.") from None
    last4 = last4_of(statement_account_id)
    try:
        return _batch_or_stale(
            lambda: create_account(
                session,
                institution_key=institution,
                institution_name=row.name,
                statement_account_id=statement_account_id,
                name=name,
                account_type=type_value,
                last4=None if last4 == "****" else last4,
                joint=joint,
                idempotency_key=idempotency_key,
            )
        )
    except AccountError as exc:
        raise ProviderSetupError(mask_account_numbers(str(exc))) from None


def create_wizard_accounts(
    session: Session,
    *,
    institution: str,
    connector: str,
    provider_key: str,
    accounts: list[NewAccount],
    idempotency_key: str,
) -> Batch:
    """Create the accounts a bank connector will sync, before any file names them, as one batch.
    The accounts join the provider, so its next sync fetches them. The institution and provider
    must already exist (step two saves them). See ledger.accounts."""
    row = session.scalars(select(Institution).where(Institution.key == institution)).first()
    if row is None:
        raise ProviderSetupError("Save the institution first. Start the wizard again.")
    provider = session.scalars(select(Provider).where(Provider.key == provider_key)).first()
    if provider is None or provider.institution_id != row.id:
        raise ProviderSetupError("Save the bank login first. Start the wizard again.")
    try:
        statement_id_is_last4 = get_connector(connector).last4_is_statement_id
    except ConnectorError:
        raise ProviderSetupError("Choose a bank from the list.") from None
    try:
        return _batch_or_stale(
            lambda: create_accounts_without_alias(
                session,
                institution_key=institution,
                institution_name=row.name,
                accounts=accounts,
                last4_is_statement_id=statement_id_is_last4,
                provider_id=provider.id,
                idempotency_key=idempotency_key,
            )
        )
    except AccountError as exc:
        raise ProviderSetupError(mask_account_numbers(str(exc))) from None
    except ValueError:
        raise ProviderSetupError("Choose an account type from the list.") from None


def map_existing_account(
    session: Session,
    *,
    institution: str,
    statement_account_id: str,
    account_id: int,
    idempotency_key: str,
) -> Batch:
    """Map a statement account to an existing account, as a reissued card's alias."""
    try:
        return _batch_or_stale(
            lambda: add_alias(
                session,
                account_id=account_id,
                institution_key=institution,
                statement_account_id=statement_account_id,
                idempotency_key=idempotency_key,
            )
        )
    except AccountError as exc:
        raise ProviderSetupError(mask_account_numbers(str(exc))) from None


def _key_value(value: str, what: str) -> str:
    key = value.strip().lower()
    if not KEY_PATTERN.fullmatch(key):
        raise ProviderSetupError(
            f"The {what} is 1 to 32 lowercase letters, digits, - or _, "
            "starting with a letter or digit."
        )
    return key


def _batch_or_stale(write: Callable[[], Batch]) -> Batch:
    try:
        return write()
    except BatchError as exc:
        raise ProviderSetupError(STALE, conflict=True) from exc


def _provider_write[T](write: Callable[[], T]) -> T:
    try:
        return write()
    except ProviderError as exc:
        raise ProviderSetupError(str(exc)) from None
    except StalePreview as exc:
        raise ProviderSetupError(STALE, conflict=True) from exc
    except BatchError as exc:
        raise ProviderSetupError(str(exc), conflict=True) from exc
