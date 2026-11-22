"""What the Data sync page and the add-provider wizard read: each institution with its providers and
accounts, each provider's latest sync run, and whether a provider can be synced. Read-only."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.ingest.connectors.config import list_providers
from omsync.ledger.models import Account, AccountType, Institution
from omsync.ledger.sync_runs import RecentSync, latest_sync_runs, recent_sync_runs

RECENT_SYNCS = 5


@dataclass(frozen=True)
class AccountLine:
    id: int
    name: str
    last4: str | None
    alias_last4: str | None
    type: AccountType
    joint: bool
    closed: bool


@dataclass(frozen=True)
class ProviderLine:
    """A provider with the accounts it fetches and its latest sync run."""

    key: str
    name: str
    connector_key: str
    connector_enabled: bool
    vault_item_id: str | None
    account_ids: tuple[int, ...]
    last_sync: RecentSync | None


@dataclass(frozen=True)
class InstitutionGroup:
    """An institution with its providers, and every account it holds in the order they were made."""

    key: str
    name: str
    providers: tuple[ProviderLine, ...]
    accounts: tuple[AccountLine, ...]

    def provider_of(self, account_id: int) -> str | None:
        """The key of the provider that fetches this account, or None."""
        for provider in self.providers:
            if account_id in provider.account_ids:
                return provider.key
        return None


def bank_sync_groups(session: Session) -> list[InstitutionGroup]:
    """Each institution that has a provider or an open account, by name, with its providers and
    accounts."""
    institutions = session.scalars(
        select(Institution).order_by(Institution.name, Institution.key)
    ).all()
    last_runs = latest_sync_runs(session)
    providers: dict[str, list[ProviderLine]] = {}
    for provider in list_providers(session):
        providers.setdefault(provider.institution_key, []).append(
            ProviderLine(
                key=provider.key,
                name=provider.name,
                connector_key=provider.connector_key,
                connector_enabled=provider.enabled,
                vault_item_id=provider.vault_item_id,
                account_ids=provider.account_ids,
                last_sync=last_runs.get(provider.key),
            )
        )
    accounts: dict[int, list[AccountLine]] = {}
    for account in session.scalars(select(Account).order_by(Account.id)).all():
        accounts.setdefault(account.institution_id, []).append(_account_line(account))
    groups: list[InstitutionGroup] = []
    for institution in institutions:
        held = tuple(accounts.get(institution.id, []))
        linked = tuple(providers.get(institution.key, []))
        if not linked and not any(not account.closed for account in held):
            continue
        groups.append(
            InstitutionGroup(
                key=institution.key,
                name=institution.name,
                providers=linked,
                accounts=held,
            )
        )
    return groups


def provider_group(
    session: Session, provider_key: str
) -> tuple[InstitutionGroup, ProviderLine] | None:
    """The institution and the provider with this key, or None when there is no such provider."""
    for group in bank_sync_groups(session):
        for provider in group.providers:
            if provider.key == provider_key:
                return group, provider
    return None


def sync_refusal(provider: ProviderLine, *, has_vault: bool) -> str | None:
    """Why this provider cannot be synced from the page, or None when it can. Without a vault,
    you sign in at the bank, so no vault login is needed."""
    if not provider.connector_enabled:
        return f"{provider.key} is turned off, so it does not sync."
    if has_vault and provider.vault_item_id is None:
        return f"{provider.key} has no vault login. Edit it to choose one."
    if not provider.account_ids:
        return f"{provider.key} fetches no accounts yet. Edit it to choose accounts."
    return None


def institution_accounts(session: Session, key: str) -> list[AccountLine]:
    """The accounts at one institution, in the order they were made."""
    return [
        _account_line(account)
        for account in session.scalars(
            select(Account)
            .join(Institution, Institution.id == Account.institution_id)
            .where(Institution.key == key)
            .order_by(Account.id)
        ).all()
    ]


def _account_line(account: Account) -> AccountLine:
    return AccountLine(
        id=account.id,
        name=account.name,
        last4=account.last4,
        alias_last4=account.alias_last4,
        type=AccountType(account.type),
        joint=bool(account.joint),
        closed=bool(account.closed),
    )


def recent_syncs(session: Session, institution_key: str) -> tuple[RecentSync, ...]:
    """The last RECENT_SYNCS runs of an institution, newest first."""
    return recent_sync_runs(session, institution_key, RECENT_SYNCS)
