"""Read a provider's receipt files into the receipt table, and tell what the table already holds.

Sync and matching read a provider the same way: every file in the provider's folder is parsed, and
each receipt is saved under that provider. A receipt that another provider already holds is left
with it and counted.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.receipts.model import Receipt
from omsync.receipts.models import ReceiptRecord, save_receipt
from omsync.receipts.vendor import ReceiptVendor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileProblem:
    """A file that could not be read, or a receipt that two files describe differently. The label
    is the path under the vendor's folder, and the message never holds the file's contents."""

    label: str
    message: str


@dataclass(frozen=True)
class ProviderRead:
    """What reading a provider's folder found. receipts are the ones this provider holds, one per
    ID. changed counts the new or changed ones among them. held_by counts, per other provider ID,
    the receipts that provider already holds."""

    receipts: tuple[Receipt, ...]
    files: int
    changed: int
    held_by: Counter[int]
    problems: tuple[FileProblem, ...]


def held_receipts(session: Session, provider_id: int) -> dict[str, Path | None]:
    """The receipt IDs a provider holds, each with the file it was read from."""
    rows = session.execute(
        select(ReceiptRecord.receipt_id, ReceiptRecord.stored_path).where(
            ReceiptRecord.provider_id == provider_id
        )
    ).all()
    return {receipt_id: Path(path) if path else None for receipt_id, path in rows}


def read_provider_files(
    session: Session,
    settings: Settings,
    vendor: ReceiptVendor,
    *,
    provider_id: int,
    provider_key: str,
) -> ProviderRead:
    """Parse every file in the provider's folder and save its receipts under the provider. A
    missing folder has no files. Commits."""
    folder = vendor.provider_folder(settings.receipts_dir, provider_key)
    files = vendor.discover(folder) if folder.is_dir() else []
    vendor_folder = settings.receipts_dir / vendor.folder
    by_id: dict[str, tuple[Receipt, Path]] = {}
    conflicts: set[str] = set()
    problems: list[FileProblem] = []
    for path in files:
        label = path.relative_to(vendor_folder).as_posix()
        try:
            parsed = vendor.parse(path, provider_key)
        except ValueError as exc:
            problems.append(FileProblem(label, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001  one bad file must not hide the others
            logger.warning("Could not read receipt file (%s)", type(exc).__name__)
            problems.append(FileProblem(label, f"could not be read ({type(exc).__name__})"))
            continue
        for receipt in parsed:
            known = by_id.get(receipt.receipt_id)
            if known is None:
                by_id[receipt.receipt_id] = (receipt, path)
            elif known[0].revision != receipt.revision:
                conflicts.add(receipt.receipt_id)
    for receipt_id in sorted(conflicts):
        by_id.pop(receipt_id, None)
        problems.append(FileProblem(receipt_id, "two files describe it differently"))

    mine: list[Receipt] = []
    changed = 0
    held_by: Counter[int] = Counter()
    for receipt, path in by_id.values():
        saved = save_receipt(session, receipt, provider_id, stored_path=str(path))
        if saved.held_by is not None:
            held_by[saved.held_by] += 1
            continue
        mine.append(receipt)
        changed += saved.written
    session.commit()
    return ProviderRead(tuple(mine), len(files), changed, held_by, tuple(problems))
