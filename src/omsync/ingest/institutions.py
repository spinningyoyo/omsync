"""What the Bank sync page and the add-provider wizard read: each institution with its providers and
accounts, each provider's latest sync run, and whether a provider can be synced. Read-only."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.ingest.connectors.config import list_providers
from omsync.ingest.models import SyncRun, SyncStatus
from omsync.ledger.models import Account, AccountType, Institution

RECENT_SYNCS = 5


@dataclass(frozen=True)
class RecentSync:
    """One SyncRun, as the account page's sync card shows it. message is already safe to show."""

    id: int
    status: SyncStatus
    started_at: datetime  # naive UTC, as SyncRun stores it; the web layer converts it for display
    finished_at: datetime | None
    files_fetched: int
    message: str | None


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
    last_runs = _latest_runs_by_provider(session)
    providers: dict[str, list[ProviderLine]] = {}
    for provider in list_providers(session):
        providers.setdefault(provider.institution_key, []).append(
            ProviderLine(
                key=provider.key,
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


def _latest_runs_by_provider(session: Session) -> dict[str, RecentSync]:
    """The newest sync run of each provider. Runs logged before providers existed have none."""
    newest = session.scalars(
        select(func.max(SyncRun.id))
        .where(SyncRun.provider_key.is_not(None))
        .group_by(SyncRun.provider_key)
    ).all()
    latest: dict[str, RecentSync] = {}
    for run in session.scalars(select(SyncRun).where(SyncRun.id.in_(newest))).all():
        if run.provider_key is not None:
            latest[run.provider_key] = RecentSync(
                id=run.id,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                files_fetched=run.files_fetched,
                message=run.message,
            )
    return latest


def recent_syncs(session: Session, institution_key: str) -> tuple[RecentSync, ...]:
    """The last RECENT_SYNCS runs of an institution, newest first."""
    runs = session.scalars(
        select(SyncRun)
        .where(SyncRun.institution_key == institution_key)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(RECENT_SYNCS)
    ).all()
    return tuple(
        RecentSync(
            id=run.id,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            files_fetched=run.files_fetched,
            message=run.message,
        )
        for run in runs
    )
