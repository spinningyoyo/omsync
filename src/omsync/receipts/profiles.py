"""Vendor profiles for receipt synchronization.

Each profile defines a vendor (Costco, Amazon), a folder login target, an optional Bitwarden
vault login item, and whether it is enabled for automated sync.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.core.vault import Vault
from omsync.ingest.connectors.base import Prompt
from omsync.ledger.preferences import get_pref, set_pref
from omsync.receipts.connectors import get_connector

PREFERENCE_KEY = "receipt_vendor_profiles"


@dataclass(frozen=True)
class VendorProfile:
    """One receipt vendor connection profile."""

    key: str
    vendor_key: str
    name: str
    account: str = "default"
    vault_item_id: str | None = None


DEFAULT_PROFILES: tuple[VendorProfile, ...] = ()


def list_vendor_profiles(session: Session) -> list[VendorProfile]:
    """Return all configured vendor profiles, or an empty list if none have been saved yet."""
    raw = get_pref(session, PREFERENCE_KEY, None)
    if not isinstance(raw, list):
        return []
    return [
        VendorProfile(
            key=str(item["key"]),
            vendor_key=str(item["vendor_key"]),
            name=str(item.get("name", item["key"])),
            account=str(item.get("account") or item["key"]),
            vault_item_id=item.get("vault_item_id"),
        )
        for item in raw
        if isinstance(item, dict) and "key" in item and "vendor_key" in item
    ]


def get_vendor_profile(session: Session, key: str) -> VendorProfile | None:
    """Return the profile for a key, or None if not found."""
    for profile in list_vendor_profiles(session):
        if profile.key == key:
            return profile
    return None


def save_vendor_profile(session: Session, profile: VendorProfile) -> None:
    """Save or update a vendor profile in preferences."""
    current = list_vendor_profiles(session)
    updated = [p for p in current if p.key != profile.key]
    updated.append(profile)
    updated.sort(key=lambda p: p.name.lower())
    set_pref(session, PREFERENCE_KEY, [asdict(p) for p in updated])


def delete_vendor_profile(session: Session, key: str) -> None:
    """Delete a vendor profile by key."""
    current = list_vendor_profiles(session)
    updated = [p for p in current if p.key != key]
    set_pref(session, PREFERENCE_KEY, [asdict(p) for p in updated])


def vendor_folder_path(settings: Settings, profile: VendorProfile) -> Path:
    """Return the filesystem directory for this profile's receipts."""
    folder = profile.account or profile.key
    return settings.receipts_dir / profile.vendor_key.upper() / folder


def vendor_receipt_count(settings: Settings, profile: VendorProfile) -> int:
    """Count existing receipt JSON files on disk for this profile."""
    folder = vendor_folder_path(settings, profile)
    if not folder.exists():
        return 0
    return sum(
        1
        for path in folder.rglob("*.json")
        if path.is_file() and not path.name.startswith(".") and not path.parent.name.startswith(".")
    )


def latest_receipt_date(settings: Settings, profile: VendorProfile) -> date | None:
    """Return the latest receipt date found on disk for this profile."""
    folder = vendor_folder_path(settings, profile)
    if not folder.exists():
        return None
    latest: date | None = None
    for path in folder.rglob("*.json"):
        if not path.is_file() or path.name.startswith(".") or path.parent.name.startswith("."):
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()
            if latest is None or mtime > latest:
                latest = mtime
        except OSError:
            continue
    return latest


def sync_vendor_profile(
    session: Session,
    settings: Settings,
    profile: VendorProfile,
    vault: Vault | None,
    prompt: Prompt,
) -> list[Path]:
    """Download new receipts for a vendor profile using automated lookback."""
    connector = get_connector(profile.vendor_key)
    latest = latest_receipt_date(settings, profile)
    today = date.today()
    start = (latest - timedelta(days=14)) if latest else (today - timedelta(days=90))
    return connector.fetch(
        settings,
        prompt,
        start=start,
        end=today,
        account=profile.account or profile.key,
        vault=vault,
        vault_item_id=profile.vault_item_id,
    )
