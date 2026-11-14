"""The counterparty of every transaction: the contact, and the contact's account, money moved to or
from.

Recognition reads each transaction's statement description and works out its counterparty, in order:

1. A contact account that the description names by its last four digits, or that a pattern tied to
   it matches: that contact and account.
2. A Zelle payment that names a person, in the wordings reconciliation reads and in the older one
   that puts the name after the confirmation, where a saved contact's name is in the name: that
   contact.
3. A contact's pattern on the side the sign names: that contact.
4. A pass-through: a transfer to an account of the owner's whose exact amount one other contact
   paid in during the seven days before or after, with no other such partner on either side. The
   contact may fund the transfer first or reimburse it afterwards. The money is the contact's, so
   the contact is the counterparty.

When a step finds several contacts or accounts it identifies nobody, and no later step guesses. A
payment identified only as the owner by name has no account, tracked or not, so nothing is stored
for it. Links come first in the design, and a link stores nothing here.

The result is stored on the transaction with an origin, as a category is: your choice outranks
recognition, and recognition never replaces it. Recognition never sets a split. It reruns as a
plan and one batch, as categorize does, so undo reverses it like any other change.

PersonCounterparties tells whether a payment is with a person, from the counterparty stored on it
or, with none stored, from the name its description gives. Categorize asks it for the rules limited
to a person.

You set the counterparty of transactions, or of one split, with set_counterparty and
set_split_counterparty. Each is one batch, the origin is manual, and clearing returns the origin to
none so that recognition may set the counterparty again.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    BatchError,
    Change,
    Preview,
    applied_batch_for_key,
    apply,
    make_preview,
)
from omsync.ledger.account_mentions import account_mentions
from omsync.ledger.contacts import (
    ContactKind,
    ContactRole,
    ContactView,
    NothingChanged,
    find_names,
    load_contacts,
    load_owner,
    tokenize,
)
from omsync.ledger.own_names import own_name_literals
from omsync.ledger.p2p_names import payment_name_text, written_name
from omsync.ledger.patterns import names_pattern
from omsync.ledger.sources import Account
from omsync.ledger.statements import SourceText, source_texts, statement_text
from omsync.ledger.transactions import (
    ContactOrigin,
    Split,
    Transaction,
    may_replace_contact,
)

PASS_THROUGH_DAYS = 7


@dataclass(frozen=True)
class Counterparty:
    """A contact, and one of its accounts when the description names one."""

    contact_id: int
    contact_account_id: int | None = None


@dataclass(frozen=True)
class CounterpartyPlan:
    """What recognition would do, computed without writing anything.

    preview is None when no transaction would change. recognized counts the transactions that
    would have a recognized counterparty afterward.
    """

    preview: Preview | None
    considered: int
    changed: int
    recognized: int


@dataclass(frozen=True)
class StoredCounterparty:
    """The counterparty stored on a transaction or a split, with the names to show for it."""

    contact_id: int | None
    contact_name: str | None
    contact_role: ContactRole | None
    account_id: int | None
    account_label: str | None
    origin: ContactOrigin


@dataclass(frozen=True)
class TransactionCounterparty:
    """The counterparty of a transaction and of each of its splits, by split ID and in position
    order. A split with none of its own belongs to its transaction's contact."""

    transaction: StoredCounterparty
    splits: tuple[tuple[int, StoredCounterparty], ...]


@dataclass(frozen=True)
class _Row:
    id: int
    posted_on: date
    amount_cents: int
    description: str
    contact_id: int | None
    contact_account_id: int | None
    origin: ContactOrigin


@dataclass(frozen=True)
class _Index:
    """The contacts, their patterns compiled, and the digits of tracked accounts."""

    contacts: tuple[ContactView, ...]
    roles: dict[int, ContactRole]
    tracked_digits: frozenset[str]
    tied: tuple[tuple[int, int, str, re.Pattern[str]], ...]  # contact, account, side, pattern
    plain: tuple[tuple[int, str, re.Pattern[str]], ...]  # contact, side, pattern
    accounts: tuple[tuple[int, int, str], ...]  # contact, account, last four digits


def plan_counterparty(session: Session) -> CounterpartyPlan:
    """Plan the recognized counterparty of every transaction. Writes nothing.

    A transaction whose origin is manual is left alone, though its stored contact still counts as
    the counterparty of a pass-through. Zero-amount transactions are skipped.
    """
    contacts = load_contacts(session)
    if not contacts:
        return CounterpartyPlan(None, 0, 0, 0)
    index = _index(session, contacts)
    rows = _rows(session)
    found: dict[int, Counterparty] = {}
    for row in rows:
        if row.origin is ContactOrigin.manual:
            if row.contact_id is not None:
                found[row.id] = Counterparty(row.contact_id, row.contact_account_id)
            continue
        other = _identify(index, row.description, row.amount_cents)
        if other is not None:
            found[row.id] = other
    found.update(_pass_through(index.roles, rows, found))

    changes: list[Change] = []
    considered = recognized = 0
    for row in rows:
        if row.origin is ContactOrigin.manual:
            continue
        considered += 1
        target = found.get(row.id)
        recognized += target is not None
        decision = _decide(row, target)
        if decision is None:
            continue
        contact_id, account_id, origin = decision
        changes.append(
            Change(
                "transaction",
                str(row.id),
                {
                    "contact_id": row.contact_id,
                    "contact_account_id": row.contact_account_id,
                    "contact_origin": row.origin.value,
                },
                {
                    "contact_id": contact_id,
                    "contact_account_id": account_id,
                    "contact_origin": origin.value,
                },
            )
        )
    preview = (
        make_preview(
            session,
            "counterparty",
            changes,
            summary={"considered": considered, "changed": len(changes)},
        )
        if changes
        else None
    )
    return CounterpartyPlan(preview, considered, len(changes), recognized)


def apply_counterparty(session: Session, plan: CounterpartyPlan, *, idempotency_key: str) -> Batch:
    """Write a recognition plan as one batch. The same key applies at most once.

    Raises BatchError if the plan has no changes, and StalePreview if the data changed since the
    plan was made.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if plan.preview is None:
        raise BatchError("nothing to identify: no transaction would change")
    return apply(session, plan.preview, idempotency_key=idempotency_key)


def transaction_counterparty(session: Session, transaction_id: int) -> TransactionCounterparty:
    """Read the counterparty stored on a transaction and its splits. Raises LookupError if the
    transaction does not exist."""
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise LookupError(f"there is no transaction {transaction_id}")
    contacts = {contact.id: contact for contact in load_contacts(session)}

    def describe(
        contact_id: int | None, account_id: int | None, origin: ContactOrigin
    ) -> StoredCounterparty:
        contact = contacts.get(contact_id) if contact_id is not None else None
        label = None
        if contact is not None and account_id is not None:
            label = next((a.label for a in contact.accounts if a.id == account_id), None)
        return StoredCounterparty(
            contact_id=contact.id if contact else None,
            contact_name=contact.name if contact else None,
            contact_role=contact.role if contact else None,
            account_id=account_id if label is not None else None,
            account_label=label,
            origin=ContactOrigin(origin),
        )

    return TransactionCounterparty(
        transaction=describe(txn.contact_id, txn.contact_account_id, txn.contact_origin),
        splits=tuple(
            (split.id, describe(split.contact_id, split.contact_account_id, split.contact_origin))
            for split in txn.splits
        ),
    )


class PersonCounterparties:
    """Whether a payment is with a person, which a rule with the person condition asks.

    A transaction whose stored counterparty is a contact is with a person when that contact is a
    person with the other role. With no stored contact it is when its description is a payment that
    names a person, in the wordings recognition reads, and the name is not your own money: none of
    your names or account nicknames, no tracked account by its digits, no saved contact that is a
    business, the owner, or the co-owner, and not only words of the owner's name: a given, middle,
    or family name, or a single letter that starts one, which may be the owner under a short
    wording. A name that no contact has is otherwise a person as written. Loads the contacts
    once, so make one for a whole run.
    """

    def __init__(self, session: Session) -> None:
        self._contacts = tuple(load_contacts(session))
        self._by_id = {contact.id: contact for contact in self._contacts}
        self._own = names_pattern(own_name_literals(session))
        self._tracked_digits = _tracked_digits(session)
        owner = load_owner(session)
        parts = {
            word
            for name in (owner.names if owner is not None else ())
            for part in (name.given, name.middle, name.family)
            for word in tokenize(part or "")
        }
        self._owner_words = frozenset(parts)
        self._owner_initials = frozenset(word[0] for word in parts)

    def with_person(
        self,
        *,
        contact_id: int | None,
        payee_raw: str,
        source: SourceText | None,
        amount_cents: int,
    ) -> bool:
        """True when the payment's counterparty is a person. source is the transaction's statement
        row, as source_texts returns it, or None when no row describes it."""
        if contact_id is not None:
            contact = self._by_id.get(contact_id)
            return contact is not None and _is_other_person(contact)
        description = statement_text(payee_raw, source)
        name_text = payment_name_text(description, amount_cents)
        written = None if name_text is None else written_name(name_text)
        if name_text is None or written is None:
            return False
        if account_mentions(description) & self._tracked_digits:
            return False
        if self._own is not None and self._own.search(name_text):
            return False
        if self._may_be_owner(written):
            return False
        named = {match.contact_id for match in find_names(name_text, self._contacts)}
        return all(_is_other_person(self._by_id[contact_id]) for contact_id in named)

    def _may_be_owner(self, written: str) -> bool:
        """True when every word is a part of the owner's name or a letter that starts one."""
        words = tokenize(written)
        return bool(words) and all(
            word in self._owner_words or (len(word) == 1 and word in self._owner_initials)
            for word in words
        )


def _is_other_person(contact: ContactView) -> bool:
    return contact.kind is ContactKind.person and contact.role is ContactRole.other


def set_counterparty(
    session: Session,
    *,
    transaction_ids: Sequence[int],
    contact_id: int | None,
    contact_account_id: int | None = None,
    idempotency_key: str,
) -> Batch:
    """Set the counterparty of several transactions at once, with the manual origin, as one batch.

    A contact_id of None with no account clears them: the origin goes back to none, so recognition
    may set them again. Transactions that already have exactly this counterparty from you are left
    out. Raises LookupError for an unknown transaction or contact, ValueError for no transactions,
    an account without a contact or one that is not the contact's, and NothingChanged when no
    transaction would change.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    ids = list(dict.fromkeys(transaction_ids))
    if not ids:
        raise ValueError("Choose at least one transaction.")
    target = _target(session, contact_id, contact_account_id)
    changes = _manual_changes(session, "transaction", Transaction, ids, target)
    if not changes:
        raise NothingChanged("Nothing has changed.")
    noun = _plural(len(changes), "transaction")
    if len(ids) == 1:
        payee = session.scalar(select(Transaction.payee).where(Transaction.id == ids[0]))
        noun = str(payee)
    label = _label(target, f"the counterparty of {noun}")
    preview = make_preview(
        session,
        "contact.set_counterparty",
        changes,
        summary={
            "label": label,
            "transaction_ids": [int(change.row_id or 0) for change in changes],
        },
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def set_split_counterparty(
    session: Session,
    split_id: int,
    *,
    contact_id: int | None,
    contact_account_id: int | None = None,
    idempotency_key: str,
) -> Batch:
    """Set the counterparty of one split, as set_counterparty does for transactions. A split with
    none of its own belongs to its transaction's contact. Raises LookupError if the split does not
    exist, and otherwise as set_counterparty does."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    split = session.get(Split, split_id)
    if split is None:
        raise LookupError(f"there is no split {split_id}")
    target = _target(session, contact_id, contact_account_id)
    changes = _manual_changes(session, "split", Split, [split_id], target)
    if not changes:
        raise NothingChanged("Nothing has changed.")
    label = _label(target, "the counterparty of a split")
    preview = make_preview(
        session,
        "contact.set_split_counterparty",
        changes,
        summary={"label": label, "transaction_id": split.transaction_id, "split_id": split_id},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


@dataclass(frozen=True)
class _Target:
    contact_id: int | None
    contact_account_id: int | None
    description: str  # who it is, for the label; empty when clearing


def _target(session: Session, contact_id: int | None, contact_account_id: int | None) -> _Target:
    if contact_id is None:
        if contact_account_id is not None:
            raise ValueError("Choose the contact this account belongs to.")
        return _Target(None, None, "")
    contacts = {contact.id: contact for contact in load_contacts(session)}
    contact = contacts.get(contact_id)
    if contact is None:
        raise LookupError(f"there is no contact {contact_id}")
    if contact_account_id is None:
        return _Target(contact_id, None, contact.name)
    account = next((a for a in contact.accounts if a.id == contact_account_id), None)
    if account is None:
        raise ValueError(f"That account is not one of {contact.name}'s accounts.")
    if contact.role is ContactRole.owner:
        return _Target(contact_id, account.id, f"your account {account.label}")
    return _Target(contact_id, account.id, f"{contact.name}, {account.label}")


def _manual_changes(
    session: Session,
    table: str,
    model: type[Transaction] | type[Split],
    ids: Sequence[int],
    target: _Target,
) -> list[Change]:
    wanted = {
        "contact_id": target.contact_id,
        "contact_account_id": target.contact_account_id,
        "contact_origin": (
            ContactOrigin.none if target.contact_id is None else ContactOrigin.manual
        ).value,
    }
    stored = {
        row.id: row
        for row in session.execute(
            select(
                model.id, model.contact_id, model.contact_account_id, model.contact_origin
            ).where(model.id.in_(ids))
        )
    }
    changes: list[Change] = []
    for row_id in ids:
        row = stored.get(row_id)
        if row is None:
            raise LookupError(f"there is no {table} {row_id}")
        before = {
            "contact_id": row.contact_id,
            "contact_account_id": row.contact_account_id,
            "contact_origin": ContactOrigin(row.contact_origin).value,
        }
        if before != wanted:
            changes.append(Change(table, str(row_id), before, dict(wanted)))
    return changes


def _label(target: _Target, what: str) -> str:
    if target.contact_id is None:
        return f"Cleared {what}"
    return f"Set {what} to {target.description}"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _identify(index: _Index, description: str, amount_cents: int) -> Counterparty | None:
    """The counterparty a description names, by the first step that finds anybody, or None.

    A step that finds more than one contact or account stops the search: nobody is identified.
    """
    side = "out" if amount_cents < 0 else "in"
    named = _named_accounts(index, description, side)
    if named:
        return _only(index, named)
    name_text = payment_name_text(description, amount_cents)
    if name_text is not None:
        ids = {match.contact_id for match in find_names(name_text, index.contacts)}
        if ids:
            return _only(index, {Counterparty(contact_id) for contact_id in ids})
    matched = {
        Counterparty(contact_id)
        for contact_id, pattern_side, pattern in index.plain
        if pattern_side == side and pattern.search(description)
    }
    return _only(index, matched) if matched else None


def _only(index: _Index, candidates: set[Counterparty]) -> Counterparty | None:
    """The one candidate, or None when there are several or it is the owner with no account."""
    if len(candidates) != 1:
        return None
    [other] = candidates
    if index.roles[other.contact_id] is ContactRole.owner and other.contact_account_id is None:
        return None
    return other


def _named_accounts(index: _Index, description: str, side: str) -> set[Counterparty]:
    mentioned = account_mentions(description) - index.tracked_digits
    found = {
        Counterparty(contact_id, account_id)
        for contact_id, account_id, last4 in index.accounts
        if last4 in mentioned
    }
    found.update(
        Counterparty(contact_id, account_id)
        for contact_id, account_id, pattern_side, pattern in index.tied
        if pattern_side == side and pattern.search(description)
    )
    return found


def _pass_through(
    roles: dict[int, ContactRole], rows: Sequence[_Row], found: dict[int, Counterparty]
) -> dict[int, Counterparty]:
    """The transfers to the owner's accounts that carry one other contact's money.

    A transfer qualifies when exactly one payment of the same amount from a contact (not the owner
    or the co-owner) posted within 7 days of it, before or after and the same day included, and
    that payment has no other transfer to pair with. The window is counted once for both
    directions, so a second payment on either side makes the transfer ambiguous. The transfer's
    counterparty is that contact. A transfer you set by hand is left alone.
    """
    payments: dict[int, list[_Row]] = defaultdict(list)
    transfers: list[_Row] = []
    for row in rows:
        other = found.get(row.id)
        if other is None:
            continue
        role = roles[other.contact_id]
        if row.amount_cents > 0 and role is ContactRole.other:
            payments[row.amount_cents].append(row)
        elif (
            row.amount_cents < 0
            and role is ContactRole.owner
            and other.contact_account_id is not None
            and row.origin is not ContactOrigin.manual
        ):
            transfers.append(row)
    partners = {
        row.id: [
            payment
            for payment in payments.get(-row.amount_cents, ())
            if abs((row.posted_on - payment.posted_on).days) <= PASS_THROUGH_DAYS
        ]
        for row in transfers
    }
    taken: dict[int, int] = defaultdict(int)
    for matches in partners.values():
        for payment in matches:
            taken[payment.id] += 1
    result: dict[int, Counterparty] = {}
    for row in transfers:
        matches = partners[row.id]
        if len(matches) == 1 and taken[matches[0].id] == 1:
            result[row.id] = Counterparty(found[matches[0].id].contact_id)
    return result


def _decide(
    row: _Row, target: Counterparty | None
) -> tuple[int | None, int | None, ContactOrigin] | None:
    """The contact, account, and origin a transaction should have, or None to leave it alone."""
    if target is not None:
        stored = (row.contact_id, row.contact_account_id, row.origin)
        wanted = (target.contact_id, target.contact_account_id, ContactOrigin.recognized)
        if stored == wanted or not may_replace_contact(ContactOrigin.recognized, row.origin):
            return None
        return wanted
    if row.origin is ContactOrigin.recognized:
        # What recognized this counterparty no longer does, so it goes back to unknown.
        return None, None, ContactOrigin.none
    return None


def _tracked_digits(session: Session) -> frozenset[str]:
    return frozenset(
        digits
        for row in session.execute(select(Account.last4, Account.alias_last4))
        for digits in row
        if digits
    )


def _index(session: Session, contacts: Sequence[ContactView]) -> _Index:
    tied: list[tuple[int, int, str, re.Pattern[str]]] = []
    plain: list[tuple[int, str, re.Pattern[str]]] = []
    accounts: list[tuple[int, int, str]] = []
    for contact in contacts:
        accounts.extend(
            (contact.id, account.id, account.last4)
            for account in contact.accounts
            if account.last4 is not None
        )
        for pattern in contact.patterns:
            compiled = re.compile(pattern.pattern, re.IGNORECASE)
            if pattern.contact_account_id is None:
                plain.append((contact.id, pattern.side, compiled))
            else:
                tied.append((contact.id, pattern.contact_account_id, pattern.side, compiled))
    return _Index(
        contacts=tuple(contacts),
        roles={contact.id: contact.role for contact in contacts},
        tracked_digits=_tracked_digits(session),
        tied=tuple(tied),
        plain=tuple(plain),
        accounts=tuple(accounts),
    )


def _rows(session: Session) -> list[_Row]:
    stored = session.execute(
        select(
            Transaction.id,
            Transaction.posted_on,
            Transaction.amount_cents,
            Transaction.payee_raw,
            Transaction.contact_id,
            Transaction.contact_account_id,
            Transaction.contact_origin,
        )
        .where(Transaction.amount_cents != 0)
        .order_by(Transaction.id)
    ).all()
    sources = source_texts(session, [row.id for row in stored])
    return [
        _Row(
            id=row.id,
            posted_on=row.posted_on,
            amount_cents=row.amount_cents,
            description=statement_text(row.payee_raw, sources.get(row.id)),
            contact_id=row.contact_id,
            contact_account_id=row.contact_account_id,
            origin=ContactOrigin(row.contact_origin),
        )
        for row in stored
    ]
