"""What the Data sync page reads: its provider cards of both kinds, from the database, the name of
a provider's vault login for its edit dialog, and the sync status region."""

from fastapi import Request
from sqlalchemy.orm import Session

from omsync.core.vault import is_locked
from omsync.ingest.institutions import bank_sync_groups, provider_group
from omsync.ledger.sync_runs import RecentSync, latest_sync_runs, provider_names
from omsync.receipts.profiles import (
    VendorProfile,
    latest_receipt_date,
    list_vendor_profiles,
    vendor_receipt_count,
)
from omsync.web.settings_views import (
    InstitutionGroupView,
    ProviderCardView,
    institution_group_view,
    provider_card_view,
    receipt_card_view,
)
from omsync.web.sync_runner import JobPhase
from omsync.web.sync_views import SyncStatusView, sync_status_view
from omsync.web.vault_logins import login_label, vault_login_name


def bank_groups(request: Request, db: Session) -> list[InstitutionGroupView]:
    vault = request.app.state.vault
    locked = is_locked(vault)
    busy = _busy(request)
    views: list[InstitutionGroupView] = []
    for group in bank_sync_groups(db):
        logins = {
            provider.key: login_label(vault, provider.vault_item_id) for provider in group.providers
        }
        views.append(institution_group_view(group, logins, locked, busy))
    return views


def receipt_cards(request: Request, db: Session) -> list[ProviderCardView]:
    runs = latest_sync_runs(db)
    return [
        _receipt_card(request, db, profile, runs.get(profile.key))
        for profile in list_vendor_profiles(db)
    ]


def provider_card(request: Request, db: Session, provider_key: str) -> ProviderCardView:
    """One provider's card, of either kind. Raises LookupError when there is no such provider."""
    for profile in list_vendor_profiles(db):
        if profile.key == provider_key:
            run = latest_sync_runs(db).get(provider_key)
            return _receipt_card(request, db, profile, run)
    found = provider_group(db, provider_key)
    if found is None:
        raise LookupError(provider_key)
    group, provider = found
    vault = request.app.state.vault
    return provider_card_view(
        group,
        provider,
        login_label(vault, provider.vault_item_id),
        locked=is_locked(vault),
        busy=_busy(request),
    )


def provider_login_name(request: Request, db: Session, provider_key: str) -> str:
    """The name of one provider's vault login, for its edit dialog, searched by the provider's own
    names. Raises LookupError when there is no such provider."""
    for profile in list_vendor_profiles(db):
        if profile.key == provider_key:
            item_id = profile.vault_item_id
            hints = (profile.name, profile.vendor_key, profile.key)
            break
    else:
        found = provider_group(db, provider_key)
        if found is None:
            raise LookupError(provider_key)
        group, provider = found
        item_id = provider.vault_item_id
        hints = (provider.key, group.name, group.key)
    return vault_login_name(request.app.state.vault, item_id, request.app.state.login_names, hints)


def all_cards(
    groups: list[InstitutionGroupView], receipts: list[ProviderCardView]
) -> list[ProviderCardView]:
    return [card for group in groups for card in group.providers] + receipts


def status_view(request: Request, db: Session) -> SyncStatusView:
    return sync_status_view(request.app.state.sync_runner.view(), provider_names(db))


def _receipt_card(
    request: Request, db: Session, profile: VendorProfile, run: RecentSync | None
) -> ProviderCardView:
    vault = request.app.state.vault
    return receipt_card_view(
        profile,
        login_label(vault, profile.vault_item_id),
        vendor_receipt_count(db, profile),
        latest_receipt_date(db, profile),
        locked=is_locked(vault),
        busy=_busy(request),
        run=run,
    )


def _busy(request: Request) -> bool:
    return request.app.state.sync_runner.view().phase is JobPhase.running
