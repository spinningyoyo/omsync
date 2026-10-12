"""Rental settings for the rental export: which accounts are rental properties, and which categories
count as rental expenses.

Both are view settings, so they are stored as preferences and written directly, not as batches
(see ledger/preferences.py). Category names are yours: a category counts as rental when its name
matches one of the names here exactly, ignoring surrounding spaces.
"""

from collections.abc import Sequence

from sqlalchemy.orm import Session

from omsync.ledger.models import Account
from omsync.ledger.preferences import get_pref, set_pref

RENTAL_ACCOUNTS = "rental_accounts"
RENTAL_CATEGORIES = "rental_categories"
DEFAULT_RENTAL_CATEGORIES: tuple[str, ...] = ("Supply", "Repair", "Utility", "Operations")


def get_rental_accounts(session: Session) -> dict[int, str]:
    """Rental account IDs mapped to their property labels, in the order they were added."""
    return {int(key): label for key, label in _stored_accounts(session).items()}


def set_rental_account(session: Session, account_id: int, label: str) -> None:
    """Mark an account as a rental property with this label, or relabel it.

    Raises ValueError when the label is blank or the account does not exist.
    """
    name = label.strip()
    if not name:
        raise ValueError("a rental property needs a label")
    if session.get(Account, account_id) is None:
        raise ValueError(f"there is no account {account_id}")
    accounts = _stored_accounts(session)
    accounts[str(account_id)] = name
    set_pref(session, RENTAL_ACCOUNTS, accounts)


def remove_rental_account(session: Session, account_id: int) -> bool:
    """Stop treating an account as a rental property. Its transactions stay as they are.

    Returns False when the account was not a rental property.
    """
    accounts = _stored_accounts(session)
    if str(account_id) not in accounts:
        return False
    del accounts[str(account_id)]
    set_pref(session, RENTAL_ACCOUNTS, accounts)
    return True


def get_rental_categories(session: Session) -> list[str]:
    """The category names that count as rental expenses. The defaults until you set your own."""
    stored = get_pref(session, RENTAL_CATEGORIES, None)
    if stored is None:
        return list(DEFAULT_RENTAL_CATEGORIES)
    return list(stored)


def set_rental_categories(session: Session, names: Sequence[str]) -> list[str]:
    """Replace the rental category names. Blank names and repeats are dropped, order is kept.

    Returns the names saved. Raises ValueError when no name is left.
    """
    cleaned = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not cleaned:
        raise ValueError("give at least one rental category name")
    set_pref(session, RENTAL_CATEGORIES, cleaned)
    return cleaned


def _stored_accounts(session: Session) -> dict[str, str]:
    """A fresh copy of the stored mapping, so callers can change it before saving it."""
    return dict(get_pref(session, RENTAL_ACCOUNTS, {}))


__all__ = [
    "DEFAULT_RENTAL_CATEGORIES",
    "get_rental_accounts",
    "get_rental_categories",
    "remove_rental_account",
    "set_rental_account",
    "set_rental_categories",
]
