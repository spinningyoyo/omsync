"""What every provider sync shares: the lock, and the SyncRun that records it.

A run starts as running and ends as succeeded, failed, needs_user_action, or cancelled. A sync
never lets a connector's exception escape: failure_of turns it into a status and a message that is
safe to show.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from omsync.core.locks import LockBusy, exclusive_lock
from omsync.core.vault import VaultError, VaultLocked
from omsync.ingest.connectors.base import ConnectorError, NeedsUserAction, SyncCancelled
from omsync.ledger.sync_runs import SyncRun, SyncStatus

MESSAGE_LIMIT = 200


class SyncBusy(Exception):
    """Another sync is running on this machine. Nothing was fetched or written."""


@contextmanager
def sync_lock(path: Path) -> Iterator[None]:
    """The sync lock, as SyncBusy when another sync holds it."""
    try:
        with exclusive_lock(path):
            yield
    except LockBusy:
        raise SyncBusy("another sync is already running") from None


def start_run(session: Session, provider_id: int) -> SyncRun:
    """Record that a sync of the provider started."""
    run = SyncRun(
        provider_id=provider_id, started_at=utc_now(), status=SyncStatus.running, items_fetched=0
    )
    session.add(run)
    session.commit()
    return run


def finish_run(
    session: Session, run: SyncRun, status: SyncStatus, items: int, message: str | None
) -> None:
    """Record how a sync ended."""
    run.status = status
    run.finished_at = utc_now()
    run.items_fetched = items
    run.message = clip(message)
    session.commit()


def failure_of(exc: Exception) -> tuple[SyncStatus, str]:
    """The status and the message to show for an exception that ended a fetch."""
    if isinstance(exc, SyncCancelled):
        return SyncStatus.cancelled, str(exc)
    if isinstance(exc, NeedsUserAction):
        return SyncStatus.needs_user_action, clip(str(exc)) or "the connector needs you"
    if isinstance(exc, VaultLocked):
        return SyncStatus.needs_user_action, "the vault is locked; unlock it and sync again"
    if isinstance(exc, ConnectorError | VaultError):
        return SyncStatus.failed, clip(str(exc)) or "the connector failed"
    # The exception text of an unexpected error can hold anything, so only its type is kept.
    return SyncStatus.failed, f"unexpected error ({type(exc).__name__})"


def clip(text: str | None) -> str | None:
    if text is None:
        return None
    clipped = text.strip()[:MESSAGE_LIMIT]
    return clipped or None


def utc_now() -> datetime:
    # Naive UTC, to match the other timestamps in the database.
    return datetime.now(UTC).replace(tzinfo=None)
