"""Sync providers of every kind: a bank's statements or a receipt vendor's receipts.

sync_one syncs one provider, and sync_all the enabled banks first, then every receipt provider.
Each holds the one sync lock for its whole length, so a sync started elsewhere waits for none of it
and is refused. Each provider sync writes one SyncRun, and every failure becomes the run's status
and a message that is safe to show.

A receipt sync downloads with the vendor's connector, then reads every receipt file in the
provider's folder into the receipt table. A file that cannot be read fails the run: the receipts
that were saved stay, and the message says how many files were not. A receipt that another provider
already holds stays with it, and the message says so: the run still succeeded.
"""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.locks import SYNC_LOCK_NAME
from omsync.core.settings import Settings
from omsync.core.vault import Vault, VaultError
from omsync.ingest.connectors.base import ConnectorError, DateRange, Prompt
from omsync.ingest.connectors.config import enabled_provider_keys
from omsync.ingest.institutions import provider_group, sync_refusal
from omsync.ingest.runs import failure_of, finish_run, start_run, sync_lock
from omsync.ingest.sync import SyncResult, bank_sync
from omsync.ledger.models import Provider, ProviderKind, SyncRun, SyncStatus, Transaction
from omsync.receipts.connectors import get_connector
from omsync.receipts.profiles import (
    VendorProfile,
    get_vendor_profile,
    latest_receipt_date,
    list_vendor_profiles,
    to_profile,
)
from omsync.receipts.store import held_receipts, read_provider_files
from omsync.receipts.vendors import get_vendor

logger = logging.getLogger(__name__)

LOOKBACK = timedelta(days=14)
FIRST_FETCH = timedelta(days=90)

ALL = "*"


@dataclass(frozen=True)
class ProviderSync:
    """What sync_all did for one provider. A provider that could not run at all, such as one with
    no accounts, writes no SyncRun, so its error is set instead of result."""

    provider_key: str
    result: SyncResult | None
    error: str | None


ProgressCallback = Callable[[str, ProviderSync | None], None]
"""Called as sync_all reaches each provider: (key, None) when its sync starts, then (key, outcome)
when it ends, with the outcome sync_all records for it."""


@dataclass(frozen=True)
class SyncPlan:
    """What a sync of one scope runs. refusal is why it cannot start, or None. needs_vault says
    whether the vault must be unlocked first."""

    bank_keys: list[str]
    receipt_keys: list[str]
    refusal: str | None
    needs_vault: bool


def receipt_provider_keys(session: Session) -> list[str]:
    """The keys of the receipt providers, by name: the order sync_all runs them in. A receipt
    provider cannot be turned off."""
    return [profile.key for profile in list_vendor_profiles(session)]


def plan_sync(session: Session, scope: str, *, has_vault: bool) -> SyncPlan:
    """The providers a sync of scope runs: ALL, or one provider's key. Raises LookupError when
    the scope names no provider."""
    if scope == ALL:
        banks = enabled_provider_keys(session)
        receipts = receipt_provider_keys(session)
        if not banks and not receipts:
            return SyncPlan([], [], "Nothing is turned on to sync.", False)
        logins = any(
            profile.vault_item_id
            for profile in list_vendor_profiles(session)
            if profile.key in receipts
        )
        return SyncPlan(banks, receipts, None, has_vault and (bool(banks) or logins))
    profile = get_vendor_profile(session, scope)
    if profile is not None:
        return SyncPlan([], [scope], None, has_vault and bool(profile.vault_item_id))
    found = provider_group(session, scope)
    if found is None:
        raise LookupError(scope)
    return SyncPlan([scope], [], sync_refusal(found[1], has_vault=has_vault), has_vault)


def sync_one(
    session: Session,
    settings: Settings,
    *,
    provider_key: str,
    vault: Vault | None,
    prompt: Prompt,
    period: DateRange | None = None,
) -> SyncResult:
    """Sync one provider of either kind, holding the sync lock for its whole length.

    Raises SyncBusy if another sync holds the lock, and ConnectorError if the provider is unknown,
    or is a bank that is turned off or cannot run (see ingest.sync.bank_sync). A failed fetch does
    not raise: it sets the result's status.
    """
    with sync_lock(settings.data_dir / SYNC_LOCK_NAME):
        return _sync(session, settings, provider_key, vault=vault, prompt=prompt, period=period)


def sync_all(
    session: Session,
    settings: Settings,
    *,
    vault: Vault | None,
    prompt: Prompt,
    period: DateRange | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[ProviderSync]:
    """Sync every enabled bank, in institution key then provider key order, then every receipt
    provider by name, one after another. Each runs for the given period, or its own default.

    A provider that fails, or cannot run, does not stop the others. Cancelling through the prompt
    does: no provider starts after it, so only the providers that started have an entry in the
    list. Raises SyncBusy, before anything is fetched, if another sync holds the lock.
    """
    outcomes: list[ProviderSync] = []
    with sync_lock(settings.data_dir / SYNC_LOCK_NAME):
        for key in [*enabled_provider_keys(session), *receipt_provider_keys(session)]:
            if prompt.cancelled():
                break
            if on_progress is not None:
                on_progress(key, None)
            outcome = _guarded(session, settings, key, vault=vault, prompt=prompt, period=period)
            outcomes.append(outcome)
            if on_progress is not None:
                on_progress(key, outcome)
    return outcomes


def _guarded(
    session: Session,
    settings: Settings,
    key: str,
    *,
    vault: Vault | None,
    prompt: Prompt,
    period: DateRange | None,
) -> ProviderSync:
    try:
        result = _sync(session, settings, key, vault=vault, prompt=prompt, period=period)
    except ConnectorError as exc:
        return ProviderSync(key, None, str(exc))
    except Exception as exc:  # one provider's crash must not stop the rest
        session.rollback()
        logger.exception("Sync of provider %s failed unexpectedly", key)
        return ProviderSync(key, None, f"unexpected error ({type(exc).__name__})")
    return ProviderSync(key, result, None)


def _sync(
    session: Session,
    settings: Settings,
    provider_key: str,
    *,
    vault: Vault | None,
    prompt: Prompt,
    period: DateRange | None,
) -> SyncResult:
    """One provider's sync, with the lock already held."""
    provider = session.scalars(select(Provider).where(Provider.key == provider_key)).first()
    if provider is None:
        raise ConnectorError(f"there is no provider {provider_key!r}")
    if provider.kind is ProviderKind.bank:
        return bank_sync(
            session,
            settings,
            provider_key=provider_key,
            vault=vault,
            prompt=prompt,
            period=period,
        )
    run = start_run(session, provider.id)
    return _receipt_run(
        session, settings, run, provider, to_profile(provider), vault, prompt, period
    )


def _receipt_run(
    session: Session,
    settings: Settings,
    run: SyncRun,
    provider: Provider,
    profile: VendorProfile,
    vault: Vault | None,
    prompt: Prompt,
    period: DateRange | None,
) -> SyncResult:
    window = period or _default_period(session, profile, date.today())
    try:
        changed, unreadable, held = _fetch_and_save(
            session, settings, provider, profile, window, vault, prompt
        )
    except Exception as exc:  # a connector must never crash the caller; its error is the status
        session.rollback()
        status, message = failure_of(exc)
        if not isinstance(exc, ConnectorError | VaultError):
            logger.exception("Receipt sync of %s failed unexpectedly", profile.key)
        return _finished(session, run, status, 0, message, window)
    except BaseException:
        session.rollback()
        finish_run(session, run, SyncStatus.cancelled, 0, "stopped before it finished")
        raise
    noun = "receipt" if changed == 1 else "receipts"
    saved = f"Saved {changed} new or changed {noun}." if changed else "No new receipts."
    saved += _held_message(session, held)
    if unreadable:
        files = "file" if unreadable == 1 else "files"
        saved += f" {unreadable} receipt {files} could not be read."
        return _finished(session, run, SyncStatus.failed, changed, saved, window)
    return _finished(session, run, SyncStatus.succeeded, changed, saved, window)


def _held_message(session: Session, held: Counter[int]) -> str:
    """One sentence per other provider that already holds receipts this sync found again."""
    names = dict(
        session.execute(select(Provider.id, Provider.name).where(Provider.id.in_(held))).all()
    )
    parts = []
    for provider_id, count in sorted(held.items()):
        noun = "receipt was" if count == 1 else "receipts were"
        parts.append(
            f" {count} {noun} already fetched by {names.get(provider_id, 'another provider')}."
        )
    return "".join(parts)


def _finished(
    session: Session,
    run: SyncRun,
    status: SyncStatus,
    items: int,
    message: str,
    window: DateRange,
) -> SyncResult:
    finish_run(session, run, status, items, message)
    return SyncResult(
        run_id=run.id,
        status=status,
        period=window,
        items_fetched=items,
        message=run.message,
        report=None,
        kind=ProviderKind.receipt,
    )


def _default_period(session: Session, profile: VendorProfile, today: date) -> DateRange:
    """From 14 days before the newest stored receipt. Without one, from 14 days before the first
    transaction or 90 days ago, whichever is earlier."""
    latest = latest_receipt_date(session, profile)
    if latest is not None:
        start = latest - LOOKBACK
    else:
        first = session.scalar(select(func.min(Transaction.posted_on)))
        start = today - FIRST_FETCH if first is None else min(first - LOOKBACK, today - FIRST_FETCH)
    return DateRange(start=min(start, today), end=today)


def _fetch_and_save(
    session: Session,
    settings: Settings,
    provider: Provider,
    profile: VendorProfile,
    window: DateRange,
    vault: Vault | None,
    prompt: Prompt,
) -> tuple[int, int, Counter[int]]:
    """Download, then read every receipt file of the provider. Returns how many receipts were new
    or changed, how many files could not be read, and how many receipts other providers hold."""
    get_connector(profile.vendor_key).fetch(
        settings,
        prompt,
        provider_key=profile.key,
        held=held_receipts(session, provider.id),
        start=window.start,
        end=window.end,
        vault=vault,
        vault_item_id=profile.vault_item_id,
    )
    read = read_provider_files(
        session,
        settings,
        get_vendor(profile.vendor_key),
        provider_id=provider.id,
        provider_key=profile.key,
    )
    return read.changed, len(read.problems), read.held_by
