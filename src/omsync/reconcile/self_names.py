"""Your own names as they appear in bank descriptions, stored as the self_names preference.

The reconcile engine adds one literal, case-insensitive alternative per name to three payee
patterns, so transfers that carry your name are recognized. The list is a view setting, not
ledger data, so it is saved with set_pref and changes no transaction.
"""

from collections.abc import Iterable

from sqlalchemy.orm import Session

from omsync.ledger.preferences import get_pref, set_pref

SELF_NAMES = "self_names"
MAX_NAMES = 10
MAX_NAME_LENGTH = 80


def get_self_names(session: Session) -> list[str]:
    """Return the saved names, or an empty list when none were set."""
    value = get_pref(session, SELF_NAMES, [])
    return [str(name) for name in value] if isinstance(value, list) else []


def set_self_names(session: Session, names: Iterable[str]) -> list[str]:
    """Save the names, replacing any earlier list, and return the cleaned list.

    Blank entries and case-insensitive duplicates are dropped. Raises ValueError for more than
    10 names or a name longer than 80 characters.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = " ".join(str(raw).split())
        if not name:
            continue
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"a name can be at most {MAX_NAME_LENGTH} characters")
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        cleaned.append(name)
    if len(cleaned) > MAX_NAMES:
        raise ValueError(f"at most {MAX_NAMES} names")
    set_pref(session, SELF_NAMES, cleaned)
    return cleaned
