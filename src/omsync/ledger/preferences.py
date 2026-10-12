"""Directly saved view preferences, separate from financial batches."""

from typing import Any

from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, Session, mapped_column

from omsync.core.db import Base

JOINT_SHARE = "joint_share"
JOINT_SHARE_DEFAULT = True
SYNC_CLOSE_ON_FAILURE = "sync_close_on_failure"


class Preference(Base):
    """One view setting. The value is any JSON value, and the key is unique."""

    __tablename__ = "preference"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


def get_pref(session: Session, key: str, default: Any = None) -> Any:
    """Return the stored value for a key, or the default when it was never set."""
    row = session.get(Preference, key)
    return default if row is None else row.value


def set_pref(session: Session, key: str, value: Any) -> None:
    """Store a value for a key, replacing any earlier one, and commit it."""
    session.merge(Preference(key=key, value=value))
    session.commit()


def joint_share_on(session: Session) -> bool:
    """Whether reports count joint-account lines at half. On unless you turned it off."""
    return bool(get_pref(session, JOINT_SHARE, JOINT_SHARE_DEFAULT))


def close_browser_on_failure(session: Session) -> bool:
    """Whether a failed sync closes its browser. On unless you turned it off, so that you can look
    at the page a sync stopped on."""
    return bool(get_pref(session, SYNC_CLOSE_ON_FAILURE, True))
