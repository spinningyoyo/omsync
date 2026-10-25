"""Your own names and account nicknames as the literals the transfer patterns recognize.

The owner's names are read in every form a bank prints (see omsync.ledger.contacts.name_forms),
and each name is also kept as you typed it, so a name with punctuation the forms drop still
matches. A tracked account's nickname is the name a payment app shows for it.
"""

from sqlalchemy.orm import Session

from omsync.ledger.contacts import PersonName, list_nicknames, load_owner


def own_name_literals(session: Session) -> list[str]:
    """Return the owner's name forms and every account nickname, without case-insensitive
    duplicates. Empty when you have not been named and no account has a nickname."""
    literals: list[str] = []
    owner = load_owner(session)
    if owner is not None:
        for name in owner.names:
            literals.extend(name_literals(name))
    literals.extend(nickname.nickname for nickname in list_nicknames(session))
    seen: set[str] = set()
    unique: list[str] = []
    for literal in literals:
        if literal.casefold() not in seen:
            seen.add(literal.casefold())
            unique.append(literal)
    return unique


def name_literals(name: PersonName) -> list[str]:
    """One of your names as literals: its forms, then the name as you typed it."""
    return [*sorted(name.forms), name.text]
