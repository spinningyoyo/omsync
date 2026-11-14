"""Checks that the writes share."""

from collections.abc import Callable
from datetime import date

from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import Change, applied_batch_for_key, apply, make_preview
from omsync.plan.models import RecurringSeries


def _write(
    session: Session,
    kind: str,
    idempotency_key: str,
    summary: dict[str, object],
    build: Callable[[], list[Change]],
) -> Batch:
    """Apply one batch. A key that already applied returns its batch and builds nothing."""
    existing = applied_batch_for_key(session, idempotency_key)
    if existing is not None:
        return existing
    preview = make_preview(session, kind, build(), summary)
    return apply(session, preview, idempotency_key=idempotency_key)


def _series_or_raise(session: Session, series_id: int) -> RecurringSeries:
    row = session.get(RecurringSeries, series_id)
    if row is None:
        raise ValueError(f"there is no recurring series {series_id}")
    return row


def _check_int(value: object, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an int number of cents")


def _check_date(value: object, what: str) -> None:
    if not isinstance(value, date):
        raise TypeError(f"{what} must be a date")
