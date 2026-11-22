"""Receipt vendor profiles: a Provider of kind receipt, read as a VendorProfile and changed as
batches.

A profile names a receipt vendor (Costco, Amazon), a label, and an optional vault login. Its key
names the folder its receipts are kept in, and the receipts it holds are the rows of its provider.
Like a bank provider, creating, editing, and deleting one is a batch, and editing or deleting it is
refused while a sync holds the sync lock.
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import delete, func, select
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
from omsync.core.settings import Settings
from omsync.ingest.connectors.base import ConnectorError
from omsync.ingest.connectors.config import KEY_PATTERN, ProviderError, not_syncing
from omsync.ledger.models import Provider, ProviderKind, SyncRun
from omsync.receipts.connectors import get_connector
from omsync.receipts.models import ReceiptRecord
from omsync.receipts.vendors import get_vendor

MAX_KEY = 32


@dataclass(frozen=True)
class VendorProfile:
    """One receipt vendor connection profile."""

    key: str
    vendor_key: str
    name: str
    vault_item_id: str | None = None


def to_profile(provider: Provider) -> VendorProfile:
    return VendorProfile(
        key=provider.key,
        vendor_key=provider.connector_key,
        name=provider.name,
        vault_item_id=provider.vault_item_id,
    )


def list_vendor_profiles(session: Session) -> list[VendorProfile]:
    """Every receipt profile, by name."""
    rows = session.scalars(
        select(Provider)
        .where(Provider.kind == ProviderKind.receipt)
        .order_by(func.lower(Provider.name), Provider.key)
    ).all()
    return [to_profile(provider) for provider in rows]


def get_vendor_profile(session: Session, key: str) -> VendorProfile | None:
    """The receipt profile with this key, or None."""
    provider = session.scalars(
        select(Provider).where(Provider.key == key, Provider.kind == ProviderKind.receipt)
    ).first()
    return None if provider is None else to_profile(provider)


def new_profile_key(session: Session, vendor_key: str, name: str) -> str:
    """A key for a new profile: the name as lowercase words joined by hyphens, or the vendor's key
    when the name has none. A taken key gets -2, -3, and so on."""
    words = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    base = (words or vendor_key)[: MAX_KEY - 3].strip("-") or vendor_key
    taken = set(session.scalars(select(Provider.key).where(Provider.key.like(f"{base}%"))))
    key, number = base, 1
    while key in taken:
        number += 1
        key = f"{base}-{number}"
    return key


def create_vendor_profile(
    session: Session, profile: VendorProfile, *, idempotency_key: str
) -> Batch:
    """Add a receipt profile, as one batch. Raises ProviderError, and writes nothing, when the key
    is malformed or taken by any provider, the name is empty, or the vendor is unknown."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if not KEY_PATTERN.fullmatch(profile.key):
        raise ProviderError(
            "the key is 1 to 32 lowercase letters, digits, - or _, starting with a letter or digit"
        )
    if not profile.name.strip():
        raise ProviderError("the profile needs a name")
    try:
        get_connector(profile.vendor_key)
    except ConnectorError:
        raise ProviderError(f"there is no receipt vendor {profile.vendor_key!r}") from None
    if session.scalars(select(Provider.id).where(Provider.key == profile.key)).first():
        raise ProviderError(f"there is already a provider {profile.key!r}")
    provider_id = next_primary_key(session, Provider.__table__)
    change = Change(
        "provider",
        str(provider_id),
        None,
        {
            "id": provider_id,
            "key": profile.key,
            "kind": ProviderKind.receipt.value,
            "name": profile.name.strip(),
            "institution_id": None,
            "connector_key": profile.vendor_key,
            "vault_item_id": profile.vault_item_id,
            "settings": {},
            "enabled": True,
        },
    )
    preview = make_preview(
        session, "receipt_provider_create", [change], summary={"provider": profile.key}
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def update_vendor_profile(
    session: Session,
    settings: Settings,
    *,
    key: str,
    name: str,
    vault_item_id: str | None,
    idempotency_key: str,
) -> Batch | None:
    """Set a profile's name and vault login, as one batch. Returns None when nothing would change.
    Raises ProviderError, and writes nothing, when the profile is unknown, the name is empty, or a
    sync holds the lock."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if not name.strip():
        raise ProviderError("the profile needs a name")
    with not_syncing(settings, key):
        provider = _receipt_provider(session, key)
        before = snapshot(session, "provider", provider.id)
        if before is None:
            raise ProviderError(f"there is no receipt profile {key!r}")
        after = {**before, "name": name.strip(), "vault_item_id": vault_item_id}
        if after == before:
            return None
        preview = make_preview(
            session,
            "receipt_provider_update",
            [Change("provider", str(provider.id), before, after)],
            summary={"provider": key},
        )
        return apply(session, preview, idempotency_key=idempotency_key)


def delete_vendor_profile(
    session: Session, settings: Settings, *, key: str, idempotency_key: str
) -> Batch:
    """Delete a profile, its sync runs and its receipt rows, as one batch. Its files stay in the
    data directory. Runs and receipt rows are read back from the files or the vendor, so they are
    deleted in the batch's transaction and undo does not restore them.
    Raises ProviderError, and writes nothing, when the profile is unknown or a sync holds the
    lock."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    with not_syncing(settings, key):
        provider = _receipt_provider(session, key)
        before = snapshot(session, "provider", provider.id)
        preview = make_preview(
            session,
            "receipt_provider_delete",
            [Change("provider", str(provider.id), before, None)],
            summary={"provider": key},
        )
        session.execute(delete(SyncRun).where(SyncRun.provider_id == provider.id))
        for record in session.scalars(
            select(ReceiptRecord).where(ReceiptRecord.provider_id == provider.id)
        ):
            session.delete(record)
        session.flush()
        return apply(session, preview, idempotency_key=idempotency_key)


def _receipt_provider(session: Session, key: str) -> Provider:
    provider = session.scalars(
        select(Provider).where(Provider.key == key, Provider.kind == ProviderKind.receipt)
    ).first()
    if provider is None:
        raise ProviderError(f"there is no receipt profile {key!r}")
    return provider


def vendor_folder_path(settings: Settings, profile: VendorProfile) -> Path:
    """The directory this profile's receipt files are kept in."""
    return get_vendor(profile.vendor_key).provider_folder(settings.receipts_dir, profile.key)


def vendor_receipt_count(session: Session, profile: VendorProfile) -> int:
    """How many receipts this profile holds."""
    return int(
        session.scalar(
            select(func.count(ReceiptRecord.id))
            .join(Provider, Provider.id == ReceiptRecord.provider_id)
            .where(Provider.key == profile.key)
        )
        or 0
    )


def latest_receipt_date(session: Session, profile: VendorProfile) -> date | None:
    """The purchase date of this profile's newest receipt, or None without one."""
    return session.scalar(
        select(func.max(ReceiptRecord.purchased_on))
        .join(Provider, Provider.id == ReceiptRecord.provider_id)
        .where(Provider.key == profile.key)
    )
