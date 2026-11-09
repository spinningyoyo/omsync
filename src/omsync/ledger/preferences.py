"""Directly saved view preferences, separate from financial batches."""

from typing import Any

from sqlalchemy import JSON, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from omsync.core.db import Base
from omsync.ledger.sources import Account

JOINT_SHARE = "joint_share"
JOINT_SHARE_DEFAULT = True


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


def has_joint_accounts(session: Session) -> bool:
    """Whether any account, open or closed, is joint. Joint share changes nothing without one."""
    return session.scalar(select(Account.id).where(Account.joint).limit(1)) is not None
