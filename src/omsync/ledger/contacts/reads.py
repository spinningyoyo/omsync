"""Name forms, contact views, and how they are loaded."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import (
    select,
)
from sqlalchemy.orm import Session

from omsync.ledger.contacts.models import (
    AccountNickname,
    Contact,
    ContactAccount,
    ContactKind,
    ContactName,
    ContactPattern,
    ContactRole,
)


@dataclass(frozen=True)
class PersonName:
    """A name in parts. id is the stored row's, and takes no part in comparing names."""

    given: str
    middle: str | None = None
    family: str | None = None
    id: int | None = field(default=None, compare=False)

    @property
    def text(self) -> str:
        """The name in one string, as given, middle, and family."""
        return " ".join(part for part in (self.given, self.middle, self.family) if part)

    @property
    def forms(self) -> frozenset[str]:
        return name_forms(self.given, self.middle, self.family)


@dataclass(frozen=True)
class NameMatch:
    """A run of words in a description that reads as one contact's name. start and end are word
    positions, end exclusive."""

    contact_id: int
    start: int
    end: int


def name_forms(given: str, middle: str | None = None, family: str | None = None) -> frozenset[str]:
    """Return the forms a bank may print for a name: lowercase, single-spaced, periods dropped.

    With a family name, G is the given name, F the family name, and lowercase g, m, and f their
    initials: "G F", "F G", "g F", "F g", and "G f". With a middle name M also "G M F", "G m F",
    "F G M", and "F G m". Without a family name only the given name. Every form holds the given
    name or its initial.
    """
    given_text = _plain(given)
    middle_text = _plain(middle or "")
    family_text = _plain(family or "")
    if not given_text:
        return frozenset()
    if not family_text:
        return frozenset({given_text})
    g, f = given_text[0], family_text[0]
    forms = {
        f"{given_text} {family_text}",
        f"{family_text} {given_text}",
        f"{g} {family_text}",
        f"{family_text} {g}",
        f"{given_text} {f}",
    }
    if middle_text:
        m = middle_text[0]
        forms |= {
            f"{given_text} {middle_text} {family_text}",
            f"{given_text} {m} {family_text}",
            f"{family_text} {given_text} {middle_text}",
            f"{family_text} {given_text} {m}",
        }
    return frozenset(forms)


def find_names(text: str, contacts: Iterable["ContactView"]) -> tuple[NameMatch, ...]:
    """Find the contacts whose names a description writes, as runs of whole words.

    Words are letters and digits, with periods dropped, so "J." and "J" read alike. When the forms
    of different contacts match overlapping words, the longest match wins, so "ALEX J EXAMPLE" is
    the owner Alex Jamie Example and not the relative Jamie Example. Overlapping matches of equal
    length for different contacts identify nobody.
    """
    words = tokenize(text)
    found: list[NameMatch] = []
    for contact in contacts:
        forms = {tuple(tokenize(form)) for name in contact.names for form in name.forms}
        for form in forms:
            width = len(form)
            found.extend(
                NameMatch(contact.id, start, start + width)
                for start in range(len(words) - width + 1)
                if tuple(words[start : start + width]) == form
            )
    return _resolve(found)


def tokenize(text: str) -> list[str]:
    """Lowercase words of letters and digits, with periods dropped."""
    return re.findall(r"[^\W_]+", text.lower().replace(".", ""))


def _plain(text: str) -> str:
    return " ".join(text.lower().replace(".", "").split())


def _resolve(found: list[NameMatch]) -> tuple[NameMatch, ...]:
    """Keep the longest matches first. A match that overlaps a longer one is dropped, and
    equal-length matches of different contacts that overlap each other are all dropped."""
    ranked = sorted(found, key=lambda m: (m.start - m.end, m.start, m.contact_id))
    claimed: set[int] = set()
    accepted: list[NameMatch] = []
    index = 0
    while index < len(ranked):
        width = ranked[index].end - ranked[index].start
        tier: list[NameMatch] = []
        while index < len(ranked) and ranked[index].end - ranked[index].start == width:
            tier.append(ranked[index])
            index += 1
        free = [match for match in tier if claimed.isdisjoint(range(match.start, match.end))]
        for match in free:
            rivals = any(
                other.contact_id != match.contact_id and _overlap(match, other) for other in free
            )
            twin = any(
                other.contact_id == match.contact_id and _overlap(match, other)
                for other in accepted
            )
            if not rivals and not twin:
                accepted.append(match)
        for match in free:
            claimed.update(range(match.start, match.end))
    return tuple(sorted(accepted, key=lambda m: (m.start, m.contact_id)))


def _overlap(first: NameMatch, second: NameMatch) -> bool:
    return first.start < second.end and second.start < first.end


@dataclass(frozen=True)
class PatternView:
    """A pattern. contact_account_id is the contact's account it identifies, if it names one."""

    id: int
    side: str
    pattern: str
    contact_account_id: int | None = None


@dataclass(frozen=True)
class ContactAccountView:
    id: int
    label: str
    last4: str | None


@dataclass(frozen=True)
class ContactView:
    """A contact with its names, patterns, and accounts, ready for matching."""

    id: int
    name: str
    kind: ContactKind
    role: ContactRole
    names: tuple[PersonName, ...]
    patterns: tuple[PatternView, ...]
    accounts: tuple[ContactAccountView, ...]


@dataclass(frozen=True)
class NicknameView:
    id: int
    account_id: int
    nickname: str


def load_contacts(session: Session) -> list[ContactView]:
    """Return every contact with its names, patterns, and accounts, ordered by ID."""
    names = _grouped(session, ContactName)
    patterns = _grouped(session, ContactPattern)
    accounts = _grouped(session, ContactAccount)
    return [
        ContactView(
            id=row.id,
            name=row.name,
            kind=row.kind,
            role=row.role,
            names=tuple(
                PersonName(n.given, n.middle, n.family, id=n.id) for n in names.get(row.id, ())
            ),
            patterns=tuple(
                PatternView(p.id, p.side, p.pattern, p.contact_account_id)
                for p in patterns.get(row.id, ())
            ),
            accounts=tuple(
                ContactAccountView(a.id, a.label, a.last4) for a in accounts.get(row.id, ())
            ),
        )
        for row in session.scalars(select(Contact).order_by(Contact.id))
    ]


def load_owner(session: Session) -> ContactView | None:
    """Return the owner contact, or None when you have not been named."""
    for contact in load_contacts(session):
        if contact.role is ContactRole.owner:
            return contact
    return None


def get_owner(session: Session) -> Contact | None:
    """Return the owner contact row, or None."""
    return session.scalars(select(Contact).where(Contact.role == ContactRole.owner)).first()


def list_nicknames(session: Session) -> list[NicknameView]:
    """Return every account nickname, ordered by account and then ID."""
    return [
        NicknameView(row.id, row.account_id, row.nickname)
        for row in session.scalars(
            select(AccountNickname).order_by(AccountNickname.account_id, AccountNickname.id)
        )
    ]


def _grouped(session: Session, model: Any) -> dict[int, list[Any]]:
    grouped: dict[int, list[Any]] = {}
    for row in session.scalars(select(model).order_by(model.id)):
        grouped.setdefault(row.contact_id, []).append(row)
    return grouped
