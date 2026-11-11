"""Contacts: the people and businesses your transactions are with, and you.

A contact has a kind, a role, and a display name. A person's names are stored as given, middle, and
family names, and recognized in the forms banks print (see name_forms). Patterns recognize wordings
that no name covers, one list for payments you sent ("out") and one for payments you received
("in"); a pattern may be tied to one of the contact's accounts. A contact account is an account
outside the ledger, named in descriptions by its last four digits. You are the owner contact; a
joint account's other owner is the co-owner. Deleting a contact or an account clears the
counterparty that transactions and splits stored for it (see omsync.ledger.counterparty).

An account nickname is the name a payment app shows for a tracked account. It belongs to the
account, not to a contact, and is not an alias, which maps a statement's account ID at import.

Writes go through omsync.core.batches, so each change can be previewed and undone. The loaders
return frozen dataclasses, so reconciliation never holds a database row.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    BatchError,
    Change,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
    snapshot,
)
from omsync.core.db import Base
from omsync.ledger.categories import CategoryRule
from omsync.ledger.enums import string_enum
from omsync.ledger.sources import Account
from omsync.ledger.transactions import ContactOrigin, Split, Transaction

MAX_NAME_LENGTH = 80
MAX_NAMES_PER_CONTACT = 10
MAX_PATTERNS_PER_SIDE = 10
MAX_PATTERN_LENGTH = 200
MAX_ACCOUNTS_PER_CONTACT = 20
MAX_NICKNAMES_PER_ACCOUNT = 10
SIDES = ("out", "in")


class NothingChanged(ValueError):
    """An update asked for the values already stored. Nothing was written."""


class ContactKind(StrEnum):
    person = "person"
    business = "business"


class ContactRole(StrEnum):
    owner = "owner"
    co_owner = "co_owner"
    other = "other"


class Contact(Base):
    """A person or business, or you. At most one contact is the owner."""

    __tablename__ = "contact"
    __table_args__ = (
        Index("uq_contact_one_owner", "role", unique=True, sqlite_where=text("role = 'owner'")),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ContactKind] = mapped_column(string_enum(ContactKind))
    role: Mapped[ContactRole] = mapped_column(
        string_enum(ContactRole),
        default=ContactRole.other,
        server_default=ContactRole.other.value,
    )
    name: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ContactName(Base):
    """One name of a contact. A business or a one-word name has only `given`."""

    __tablename__ = "contact_name"
    __table_args__ = (
        UniqueConstraint("contact_id", "position", name="uq_contact_name_contact_position"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contact.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    given: Mapped[str]
    middle: Mapped[str | None]
    family: Mapped[str | None]


class ContactPattern(Base):
    """A regular expression for the wordings of a contact's payments, on one side."""

    __tablename__ = "contact_pattern"
    __table_args__ = (
        UniqueConstraint("contact_id", "side", "pattern", name="uq_contact_pattern_side_pattern"),
        CheckConstraint("side IN ('out', 'in')", name="ck_contact_pattern_side"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contact.id", ondelete="CASCADE"), index=True
    )
    side: Mapped[str]
    pattern: Mapped[str]
    contact_account_id: Mapped[int | None] = mapped_column(ForeignKey("contact_account.id"))


class ContactAccount(Base):
    """An account outside the ledger: one of the owner's untracked accounts, or a contact's own."""

    __tablename__ = "contact_account"
    __table_args__ = (
        CheckConstraint(
            "last4 IS NULL OR (length(last4) = 4 AND last4 NOT GLOB '*[^0-9]*')",
            name="ck_contact_account_last4",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contact.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str]
    last4: Mapped[str | None]


class AccountNickname(Base):
    """The name a payment app shows for a tracked account. Unique per account without regard to
    case: the column compares without case, so the unique constraint does too."""

    __tablename__ = "account_nickname"
    __table_args__ = (
        UniqueConstraint("account_id", "nickname", name="uq_account_nickname_account_nickname"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="CASCADE"), index=True
    )
    nickname: Mapped[str] = mapped_column(String(collation="NOCASE"))


# Name forms


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
            for start in range(len(words) - width + 1):
                if tuple(words[start : start + width]) == form:
                    found.append(NameMatch(contact.id, start, start + width))
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


# Views


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


# Validation


def clean_person_name(
    given: str, middle: str | None = None, family: str | None = None
) -> PersonName:
    """Check and clean a name's parts. A middle name needs a family name, and every part is at most
    80 characters. Raises ValueError otherwise."""
    parts = [" ".join(str(part or "").split()) for part in (given, middle, family)]
    clean_given, clean_middle, clean_family = parts
    if not _plain(clean_given):
        raise ValueError("Enter a name.")
    if clean_middle and not clean_family:
        raise ValueError("A middle name needs a family name too.")
    for part in parts:
        if len(part) > MAX_NAME_LENGTH:
            raise ValueError(f"A name can be at most {MAX_NAME_LENGTH} characters.")
    return PersonName(clean_given, clean_middle or None, clean_family or None)


def parse_person_name(text: str) -> PersonName:
    """Read a display name as a person's: the first word is the given name, the last the family
    name, and any words between them the middle name. One word is a given name only. Raises
    ValueError for an empty name or a part over 80 characters."""
    words = str(text).split()
    if len(words) < 2:
        return clean_person_name(" ".join(words))
    return clean_person_name(words[0], " ".join(words[1:-1]), words[-1])


def starting_names(
    name: str,
    kind: ContactKind | str,
    *,
    given: str = "",
    middle: str = "",
    family: str = "",
) -> list[PersonName]:
    """The names a new contact starts with: the parts typed, or with none the display name, read
    as a person's name for a person and whole for a business."""
    if given.strip() or middle.strip() or family.strip():
        return [clean_person_name(given, middle, family)]
    if ContactKind(kind) is ContactKind.business:
        return [clean_person_name(name)]
    return [parse_person_name(name)]


def check_pattern(pattern: str) -> None:
    """Raise ValueError unless the pattern is a valid regular expression of at most 200
    characters that cannot match the empty string, which would match every payee."""
    if not pattern:
        raise ValueError("Enter a pattern.")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"A pattern can be at most {MAX_PATTERN_LENGTH} characters.")
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{pattern!r} is not a valid pattern: {exc}") from exc
    if compiled.search(""):
        raise ValueError(f"{pattern!r} would match every payee. Make it more specific.")


def _clean_label(value: str, what: str) -> str:
    clean = " ".join(str(value).split())
    if not clean:
        raise ValueError(f"Enter {what}.")
    if len(clean) > MAX_NAME_LENGTH:
        raise ValueError(
            f"{what[0].upper()}{what[1:]} can be at most {MAX_NAME_LENGTH} characters."
        )
    return clean


# Writes


def create_contact(
    session: Session,
    *,
    name: str,
    kind: ContactKind | str,
    idempotency_key: str,
    role: ContactRole | str = ContactRole.other,
    names: Sequence[PersonName] = (),
) -> Batch:
    """Add a contact with its names. Display names are unique without regard to case, and there
    is at most one owner. The batch summary holds the new contact's ID as contact_id."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    clean_name = _clean_contact_name(session, name, except_id=None)
    contact_role = ContactRole(role)
    if contact_role is ContactRole.owner:
        _require_no_owner(session)
    cleaned = _distinct_names(
        [clean_person_name(part.given, part.middle, part.family) for part in names]
    )
    contact_id = next_primary_key(session, Contact.__table__)
    changes = [
        Change(
            "contact",
            str(contact_id),
            None,
            {
                "id": contact_id,
                "kind": ContactKind(kind).value,
                "role": contact_role.value,
                "name": clean_name,
            },
        )
    ]
    first_name_id = next_primary_key(session, ContactName.__table__)
    for position, person in enumerate(cleaned):
        name_id = first_name_id + position
        changes.append(
            Change(
                "contact_name",
                str(name_id),
                None,
                _name_values(name_id, contact_id, position, person),
            )
        )
    label = (
        f"Added {clean_name}"
        if contact_role is ContactRole.other
        else f"Added {clean_name} as {contact_role.value.replace('_', '-')}"
    )
    return _write(session, "contact.create", changes, label, idempotency_key, contact_id=contact_id)


def update_contact(
    session: Session,
    contact_id: int,
    *,
    idempotency_key: str,
    name: str | None = None,
    kind: ContactKind | str | None = None,
) -> Batch:
    """Change a contact's display name or kind. Raises LookupError if it does not exist, and
    NothingChanged when the values are already stored."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = _snapshot_or_raise(session, "contact", contact_id)
    wanted: dict[str, Any] = {}
    if name is not None:
        wanted["name"] = _clean_contact_name(session, name, except_id=contact_id)
    if kind is not None:
        wanted["kind"] = ContactKind(kind).value
    changed = {key: value for key, value in wanted.items() if current[key] != value}
    if not changed:
        raise NothingChanged("Nothing has changed.")
    before = {key: current[key] for key in changed}
    return _write(
        session,
        "contact.update",
        [Change("contact", str(contact_id), before, changed)],
        f"Updated {current['name']}",
        idempotency_key,
    )


def set_contact_role(
    session: Session, contact_id: int, role: ContactRole | str, *, idempotency_key: str
) -> Batch:
    """Make a contact the owner, the co-owner of the joint accounts, or neither. Raises
    LookupError if it does not exist and ValueError if another contact is already the owner."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = _snapshot_or_raise(session, "contact", contact_id)
    wanted = ContactRole(role)
    if current["role"] == wanted.value:
        raise NothingChanged("Nothing has changed.")
    if wanted is ContactRole.owner:
        _require_no_owner(session)
    return _write(
        session,
        "contact.set_role",
        [Change("contact", str(contact_id), {"role": current["role"]}, {"role": wanted.value})],
        f"Made {current['name']} {wanted.value.replace('_', '-')}",
        idempotency_key,
    )


def delete_contact(session: Session, contact_id: int, *, idempotency_key: str) -> Batch:
    """Remove a contact with its names, patterns, and accounts, and clear the counterparty that
    transactions and splits stored for it. Raises LookupError if it does not exist."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _snapshot_or_raise(session, "contact", contact_id)
    if session.scalar(select(CategoryRule.id).where(CategoryRule.contact_id == contact_id)):
        raise BatchError(f"A rule names {before['name']}. Change or delete the rule first.")
    account_ids = list(
        session.scalars(select(ContactAccount.id).where(ContactAccount.contact_id == contact_id))
    )
    changes = _clear_counterparty(session, contact_id=contact_id, account_ids=account_ids)
    for table, model in (
        ("contact_name", ContactName),
        ("contact_pattern", ContactPattern),
        ("contact_account", ContactAccount),
    ):
        for child_id in session.scalars(
            select(model.id).where(model.contact_id == contact_id).order_by(model.id)
        ):
            changes.append(Change(table, str(child_id), snapshot(session, table, child_id), None))
    changes.append(Change("contact", str(contact_id), before, None))
    return _write(session, "contact.delete", changes, f"Deleted {before['name']}", idempotency_key)


def merge_contacts(session: Session, *, keep_id: int, merge_id: int, idempotency_key: str) -> Batch:
    """Fold one contact into another: the merged contact's names, patterns, and accounts, and the
    counterparty of every transaction and split that named it, move to the kept contact, and the
    merged contact is deleted, all in one batch.

    A name or pattern the kept contact already has is not repeated. The kept contact keeps its
    display name and role. Raises LookupError if either does not exist, and ValueError when they
    are the same contact, when the owner would be merged into anyone, when the co-owner would be
    merged into a contact that is not a co-owner, or when the result would hold more names,
    patterns, or accounts than a contact may.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if keep_id == merge_id:
        raise ValueError("Choose a different contact to merge into.")
    keep = _snapshot_or_raise(session, "contact", keep_id)
    merged = _snapshot_or_raise(session, "contact", merge_id)
    if merged["role"] == ContactRole.owner.value:
        raise ValueError("You cannot be merged into another contact.")
    if merged["role"] == ContactRole.co_owner.value and keep["role"] != merged["role"]:
        raise ValueError("The co-owner can be merged only into another co-owner.")

    changes = _merge_names(session, keep_id, merge_id)
    changes += _merge_patterns(session, keep_id, merge_id)
    changes += _merge_accounts(session, keep_id, merge_id)
    for table, model in (("transaction", Transaction), ("split", Split)):
        changes += [
            Change(table, str(row_id), {"contact_id": merge_id}, {"contact_id": keep_id})
            for row_id in session.scalars(
                select(model.id).where(model.contact_id == merge_id).order_by(model.id)
            )
        ]
    changes += [
        Change("category_rule", str(rule_id), {"contact_id": merge_id}, {"contact_id": keep_id})
        for rule_id in session.scalars(
            select(CategoryRule.id)
            .where(CategoryRule.contact_id == merge_id)
            .order_by(CategoryRule.id)
        )
    ]
    changes.append(Change("contact", str(merge_id), merged, None))
    return _write(
        session,
        "contact.merge",
        changes,
        f"Merged {merged['name']} into {keep['name']}",
        idempotency_key,
    )


def add_name(
    session: Session,
    contact_id: int,
    *,
    given: str,
    middle: str | None = None,
    family: str | None = None,
    idempotency_key: str,
) -> Batch:
    """Add a name to a contact. Raises LookupError if the contact does not exist, and ValueError
    for a name the contact already has or more than 10 names."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    contact = _snapshot_or_raise(session, "contact", contact_id)
    person = clean_person_name(given, middle, family)
    stored = _stored_names(session, contact_id)
    _distinct_names([*stored, person])
    if len(stored) >= MAX_NAMES_PER_CONTACT:
        raise ValueError(f"A contact can have at most {MAX_NAMES_PER_CONTACT} names.")
    name_id = next_primary_key(session, ContactName.__table__)
    position = max((row.position for row in _name_rows(session, contact_id)), default=-1) + 1
    return _write(
        session,
        "contact.add_name",
        [
            Change(
                "contact_name",
                str(name_id),
                None,
                _name_values(name_id, contact_id, position, person),
            )
        ],
        f"Added the name {person.text} to {contact['name']}",
        idempotency_key,
    )


def add_owner_name(
    session: Session,
    *,
    given: str,
    middle: str | None = None,
    family: str | None = None,
    idempotency_key: str,
) -> Batch:
    """Add a name to the owner, who is created from this name when you have not been named."""
    owner = get_owner(session)
    if owner is not None:
        return add_name(
            session,
            owner.id,
            given=given,
            middle=middle,
            family=family,
            idempotency_key=idempotency_key,
        )
    person = clean_person_name(given, middle, family)
    return create_contact(
        session,
        name=person.text,
        kind=ContactKind.person,
        role=ContactRole.owner,
        names=[person],
        idempotency_key=idempotency_key,
    )


def remove_name(session: Session, name_id: int, *, idempotency_key: str) -> Batch:
    """Remove one name. Raises LookupError if it does not exist."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _snapshot_or_raise(session, "contact_name", name_id)
    contact = _snapshot_or_raise(session, "contact", before["contact_id"])
    label = PersonName(before["given"], before["middle"], before["family"]).text
    return _write(
        session,
        "contact.remove_name",
        [Change("contact_name", str(name_id), before, None)],
        f"Removed the name {label} from {contact['name']}",
        idempotency_key,
    )


def add_pattern(
    session: Session,
    contact_id: int,
    *,
    side: str,
    pattern: str,
    idempotency_key: str,
    contact_account_id: int | None = None,
) -> Batch:
    """Add a pattern for payments on one side, "out" or "in", optionally tying it to one of the
    contact's accounts. Raises LookupError if the contact does not exist, and ValueError for an
    invalid or repeated pattern, more than 10 on a side, or an account that is not the contact's."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    contact = _snapshot_or_raise(session, "contact", contact_id)
    if side not in SIDES:
        raise ValueError('a pattern side is "out" or "in"')
    if contact_account_id is not None:
        account = snapshot(session, "contact_account", contact_account_id)
        if account is None or account["contact_id"] != contact_id:
            raise ValueError("A pattern can name only an account of the same contact.")
    clean = str(pattern).strip()
    check_pattern(clean)
    stored = [
        row.pattern
        for row in session.scalars(
            select(ContactPattern).where(
                ContactPattern.contact_id == contact_id, ContactPattern.side == side
            )
        )
    ]
    if clean in stored:
        raise ValueError(f"{clean!r} is already a {side} pattern")
    if len(stored) >= MAX_PATTERNS_PER_SIDE:
        raise ValueError(f"A contact can have at most {MAX_PATTERNS_PER_SIDE} {side} patterns.")
    pattern_id = next_primary_key(session, ContactPattern.__table__)
    values = {
        "id": pattern_id,
        "contact_id": contact_id,
        "side": side,
        "pattern": clean,
        "contact_account_id": contact_account_id,
    }
    return _write(
        session,
        "contact.add_pattern",
        [Change("contact_pattern", str(pattern_id), None, values)],
        f"Added a pattern to {contact['name']}",
        idempotency_key,
    )


def remove_pattern(session: Session, pattern_id: int, *, idempotency_key: str) -> Batch:
    """Remove one pattern. Raises LookupError if it does not exist."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _snapshot_or_raise(session, "contact_pattern", pattern_id)
    contact = _snapshot_or_raise(session, "contact", before["contact_id"])
    return _write(
        session,
        "contact.remove_pattern",
        [Change("contact_pattern", str(pattern_id), before, None)],
        f"Removed a pattern from {contact['name']}",
        idempotency_key,
    )


def add_contact_account(
    session: Session,
    contact_id: int,
    *,
    label: str,
    last4: str | None = None,
    idempotency_key: str,
) -> Batch:
    """Add an account outside the ledger to a contact. Raises LookupError if the contact does not
    exist, and ValueError for a blank label, last four digits that are not four digits, or more
    than 20 accounts."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    contact = _snapshot_or_raise(session, "contact", contact_id)
    clean_label = _clean_label(label, "an account label")
    digits = (last4 or "").strip() or None
    if digits is not None and not (len(digits) == 4 and digits.isascii() and digits.isdigit()):
        raise ValueError("Enter the last four digits, such as 4821.")
    held = session.scalar(
        select(func.count())
        .select_from(ContactAccount)
        .where(ContactAccount.contact_id == contact_id)
    )
    if held >= MAX_ACCOUNTS_PER_CONTACT:
        raise ValueError(f"A contact can have at most {MAX_ACCOUNTS_PER_CONTACT} accounts.")
    account_id = next_primary_key(session, ContactAccount.__table__)
    values = {"id": account_id, "contact_id": contact_id, "label": clean_label, "last4": digits}
    return _write(
        session,
        "contact.add_account",
        [Change("contact_account", str(account_id), None, values)],
        f"Added the account {clean_label} to {contact['name']}",
        idempotency_key,
    )


def remove_contact_account(
    session: Session, contact_account_id: int, *, idempotency_key: str
) -> Batch:
    """Remove one contact account. Transactions and splits that named it keep their contact and
    lose the account, and a pattern tied to it becomes a plain pattern of the contact. Raises
    LookupError if it does not exist."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _snapshot_or_raise(session, "contact_account", contact_account_id)
    contact = _snapshot_or_raise(session, "contact", before["contact_id"])
    changes = _clear_counterparty(session, account_ids=[contact_account_id])
    for pattern_id in session.scalars(
        select(ContactPattern.id)
        .where(ContactPattern.contact_account_id == contact_account_id)
        .order_by(ContactPattern.id)
    ):
        changes.append(
            Change(
                "contact_pattern",
                str(pattern_id),
                {"contact_account_id": contact_account_id},
                {"contact_account_id": None},
            )
        )
    changes.append(Change("contact_account", str(contact_account_id), before, None))
    return _write(
        session,
        "contact.remove_account",
        changes,
        f"Removed the account {before['label']} from {contact['name']}",
        idempotency_key,
    )


def add_account_nickname(
    session: Session, account_id: int, *, nickname: str, idempotency_key: str
) -> Batch:
    """Add the name a payment app shows for a tracked account. Raises LookupError if the account
    does not exist, and ValueError for a blank or repeated nickname or more than 10."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"no account {account_id}")
    clean = _clean_label(nickname, "a nickname")
    stored = list(
        session.scalars(
            select(AccountNickname.nickname).where(AccountNickname.account_id == account_id)
        )
    )
    if any(clean.casefold() == other.casefold() for other in stored):
        raise ValueError(f"{clean!r} is already a nickname of this account")
    if len(stored) >= MAX_NICKNAMES_PER_ACCOUNT:
        raise ValueError(f"An account can have at most {MAX_NICKNAMES_PER_ACCOUNT} nicknames.")
    nickname_id = next_primary_key(session, AccountNickname.__table__)
    values = {"id": nickname_id, "account_id": account_id, "nickname": clean}
    return _write(
        session,
        "account_nickname.add",
        [Change("account_nickname", str(nickname_id), None, values)],
        f"Added the nickname {clean} to {account.name}",
        idempotency_key,
    )


def remove_account_nickname(session: Session, nickname_id: int, *, idempotency_key: str) -> Batch:
    """Remove one account nickname. Raises LookupError if it does not exist."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _snapshot_or_raise(session, "account_nickname", nickname_id)
    return _write(
        session,
        "account_nickname.remove",
        [Change("account_nickname", str(nickname_id), before, None)],
        f"Removed the nickname {before['nickname']}",
        idempotency_key,
    )


# Helpers


def _clear_counterparty(
    session: Session, *, contact_id: int | None = None, account_ids: Sequence[int] = ()
) -> list[Change]:
    """Changes that clear the counterparty of every transaction and split stored for a contact (all
    of it, with its origin) or for contact accounts (the account only). Rows are in ID order."""
    changes: list[Change] = []
    for table, model in (("transaction", Transaction), ("split", Split)):
        conditions = []
        if contact_id is not None:
            conditions.append(model.contact_id == contact_id)
        if account_ids:
            conditions.append(model.contact_account_id.in_(account_ids))
        rows = session.execute(
            select(model.id, model.contact_id, model.contact_account_id, model.contact_origin)
            .where(or_(*conditions))
            .order_by(model.id)
        )
        for row_id, row_contact, row_account, origin in rows:
            before = {
                "contact_id": row_contact,
                "contact_account_id": row_account,
                "contact_origin": ContactOrigin(origin).value,
            }
            after = dict(before)
            if contact_id is not None and row_contact == contact_id:
                after = {
                    "contact_id": None,
                    "contact_account_id": None,
                    "contact_origin": ContactOrigin.none.value,
                }
            else:
                after["contact_account_id"] = None
            changes.append(Change(table, str(row_id), before, after))
    return changes


def _merge_names(session: Session, keep_id: int, merge_id: int) -> list[Change]:
    kept = _stored_names(session, keep_id)
    seen = {_plain(person.text) for person in kept}
    position = max((row.position for row in _name_rows(session, keep_id)), default=-1) + 1
    changes: list[Change] = []
    held = len(kept)
    for row in _name_rows(session, merge_id):
        key = _plain(PersonName(row.given, row.middle, row.family).text)
        if key in seen:
            changes.append(
                Change("contact_name", str(row.id), snapshot(session, "contact_name", row.id), None)
            )
            continue
        seen.add(key)
        held += 1
        changes.append(
            Change(
                "contact_name",
                str(row.id),
                {"contact_id": merge_id, "position": row.position},
                {"contact_id": keep_id, "position": position},
            )
        )
        position += 1
    if held > MAX_NAMES_PER_CONTACT:
        raise ValueError(f"The merged contact would have more than {MAX_NAMES_PER_CONTACT} names.")
    return changes


def _merge_patterns(session: Session, keep_id: int, merge_id: int) -> list[Change]:
    def rows(contact_id: int) -> list[ContactPattern]:
        return list(
            session.scalars(
                select(ContactPattern)
                .where(ContactPattern.contact_id == contact_id)
                .order_by(ContactPattern.id)
            )
        )

    held = {side: 0 for side in SIDES}
    seen: set[tuple[str, str]] = set()
    for row in rows(keep_id):
        held[row.side] += 1
        seen.add((row.side, row.pattern))
    changes: list[Change] = []
    for row in rows(merge_id):
        if (row.side, row.pattern) in seen:
            changes.append(
                Change(
                    "contact_pattern",
                    str(row.id),
                    snapshot(session, "contact_pattern", row.id),
                    None,
                )
            )
            continue
        held[row.side] += 1
        changes.append(
            Change(
                "contact_pattern",
                str(row.id),
                {"contact_id": merge_id},
                {"contact_id": keep_id},
            )
        )
    for side, count in held.items():
        if count > MAX_PATTERNS_PER_SIDE:
            raise ValueError(
                f"the merged contact would have more than {MAX_PATTERNS_PER_SIDE} {side} patterns"
            )
    return changes


def _merge_accounts(session: Session, keep_id: int, merge_id: int) -> list[Change]:
    moving = list(
        session.scalars(
            select(ContactAccount.id)
            .where(ContactAccount.contact_id == merge_id)
            .order_by(ContactAccount.id)
        )
    )
    held = session.scalar(
        select(func.count()).select_from(ContactAccount).where(ContactAccount.contact_id == keep_id)
    )
    if held + len(moving) > MAX_ACCOUNTS_PER_CONTACT:
        raise ValueError(
            f"the merged contact would have more than {MAX_ACCOUNTS_PER_CONTACT} accounts"
        )
    return [
        Change(
            "contact_account", str(account_id), {"contact_id": merge_id}, {"contact_id": keep_id}
        )
        for account_id in moving
    ]


def _name_values(
    name_id: int, contact_id: int, position: int, person: PersonName
) -> dict[str, Any]:
    return {
        "id": name_id,
        "contact_id": contact_id,
        "position": position,
        "given": person.given,
        "middle": person.middle,
        "family": person.family,
    }


def _name_rows(session: Session, contact_id: int) -> list[ContactName]:
    return list(
        session.scalars(
            select(ContactName)
            .where(ContactName.contact_id == contact_id)
            .order_by(ContactName.position)
        )
    )


def _stored_names(session: Session, contact_id: int) -> list[PersonName]:
    return [
        PersonName(row.given, row.middle, row.family, id=row.id)
        for row in _name_rows(session, contact_id)
    ]


def _distinct_names(names: Sequence[PersonName]) -> list[PersonName]:
    """Return the names, raising ValueError when two read alike. Case, spacing, and periods do not
    tell names apart."""
    seen: set[str] = set()
    for person in names:
        key = _plain(person.text)
        if key in seen:
            raise ValueError(f"{person.text!r} is already a name of this contact")
        seen.add(key)
    return list(names)


def _clean_contact_name(session: Session, name: str, *, except_id: int | None) -> str:
    clean = _clean_label(name, "a contact name")
    for row in session.scalars(select(Contact)):
        if row.id != except_id and row.name.casefold() == clean.casefold():
            raise ValueError(f'A contact named "{row.name}" already exists.')
    return clean


def _require_no_owner(session: Session) -> None:
    owner = get_owner(session)
    if owner is not None:
        raise ValueError(f"{owner.name} is already the owner")


def _snapshot_or_raise(session: Session, table: str, row_id: int) -> dict[str, Any]:
    before = snapshot(session, table, row_id)
    if before is None:
        raise LookupError(f"no {table.replace('_', ' ')} {row_id}")
    return before


def _write(
    session: Session,
    kind: str,
    changes: list[Change],
    label: str,
    idempotency_key: str,
    **summary: Any,
) -> Batch:
    preview = make_preview(session, kind, changes, summary={"label": label, **summary})
    return apply(session, preview, idempotency_key=idempotency_key)
