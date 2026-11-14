"""Contact writes. Each one is a batch."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    func,
    or_,
    select,
)
from sqlalchemy.orm import Session

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
from omsync.ledger.categories import CategoryRule
from omsync.ledger.contacts.models import (
    MAX_ACCOUNTS_PER_CONTACT,
    MAX_NAMES_PER_CONTACT,
    MAX_NICKNAMES_PER_ACCOUNT,
    MAX_PATTERNS_PER_SIDE,
    SIDES,
    AccountNickname,
    Contact,
    ContactAccount,
    ContactKind,
    ContactName,
    ContactPattern,
    ContactRole,
    NothingChanged,
)
from omsync.ledger.contacts.reads import PersonName, _plain, get_owner
from omsync.ledger.contacts.validation import _clean_label, check_pattern, clean_person_name
from omsync.ledger.sources import Account
from omsync.ledger.transactions import ContactOrigin, Split, Transaction


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

    held = dict.fromkeys(SIDES, 0)
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
