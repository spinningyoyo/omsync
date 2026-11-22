"""Receipt vendor profiles: a Provider of kind receipt, read as a VendorProfile.

A profile names a receipt vendor (Costco, Amazon), a label, and an optional vault login. Its key
names the folder its receipts are kept in, and the receipts it holds are the rows of its provider.
Creating, editing, and deleting one is in omsync.sync.providers, as for a bank provider.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.ledger.models import Provider, ProviderKind
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
