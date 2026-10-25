"""View data for the Contacts page and the other-side pickers: rows and labels the templates draw.

The pages are read-only views of contacts-package data. Every write is a domain function called by
the routes, and every figure comes from omsync.reports.contacts, never from arithmetic here.
"""

import re
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.ledger.contacts import (
    ContactAccountView,
    ContactKind,
    ContactRole,
    ContactView,
    load_contacts,
)
from omsync.ledger.preferences import joint_share_on
from omsync.ledger.transactions import Transaction
from omsync.reconcile.own_names import name_literals
from omsync.reconcile.patterns import names_pattern
from omsync.reports.contacts import ContactBalance, Direction, contact_balance, contact_balances

DIRECTION_LABELS = {
    Direction.owes_you: "Owes you",
    Direction.you_owe: "You owe",
    Direction.settled: "Settled",
}
ROLE_LABELS = {ContactRole.owner: "You", ContactRole.co_owner: "Co-owner", ContactRole.other: ""}
KIND_LABELS = {ContactKind.person: "Person", ContactKind.business: "Business"}
SIDE_LABELS = {"out": "You paid", "in": "You received"}
ORIGIN_LABELS = {"none": "Not identified", "recognized": "Recognized", "manual": "You set this"}
NEW_CONTACT = "new"


@dataclass(frozen=True)
class NameRow:
    id: int
    text: str
    forms: str
    matches: int | None  # transactions whose payee text it matches; shown on your page only
    remove_key: str


@dataclass(frozen=True)
class PatternRow:
    id: int
    side: str
    pattern: str
    remove_key: str


@dataclass(frozen=True)
class AccountRow:
    """An account outside the ledger, with the patterns tied to it."""

    id: int
    label: str
    digits: str
    patterns: tuple[PatternRow, ...]
    remove_key: str
    add_pattern_key: str


@dataclass(frozen=True)
class ContactEditor:
    """What the name, account, and pattern editors of one contact draw."""

    id: int
    name: str
    is_owner: bool
    names: tuple[NameRow, ...]
    accounts: tuple[AccountRow, ...]
    loose_patterns: tuple[PatternRow, ...]  # patterns that name no account
    account_choices: tuple[tuple[int, str], ...]
    add_name_key: str
    add_account_key: str
    add_pattern_key: str


@dataclass(frozen=True)
class ContactRow:
    """One line of the contacts list. balance_cents is None for a contact that keeps none."""

    id: int
    name: str
    kind: str
    role: str
    balance_cents: int | None
    direction: str
    open_items: int


@dataclass(frozen=True)
class OpenItemRow:
    txn_id: int
    on: str
    payee: str
    amount_cents: int
    open_cents: int


@dataclass(frozen=True)
class LineRow:
    txn_id: int
    on: str
    payee: str
    amount_cents: int
    cleared_cents: int
    is_split: bool


@dataclass(frozen=True)
class BalanceView:
    cents: int
    direction: str
    direction_key: str
    open_items: tuple[OpenItemRow, ...]
    lines: tuple[LineRow, ...]


@dataclass(frozen=True)
class ContactDetail:
    editor: ContactEditor
    kind: str
    kind_value: str
    role: str
    role_value: str
    is_owner: bool
    balance: BalanceView | None  # None for you and for a co-owner
    merge_choices: tuple[tuple[int, str], ...]
    edit_key: str
    role_key: str
    delete_key: str
    merge_key: str


@dataclass(frozen=True)
class Choice:
    """One option of an other-side picker. value is "contact" or "contact:account"."""

    value: str
    label: str


@dataclass(frozen=True)
class ChoiceGroup:
    label: str
    choices: tuple[Choice, ...]


# Pages


def list_context(db: Session) -> dict[str, object]:
    """The list view: every contact, you first, and whether you have been named yet."""
    contacts = load_contacts(db)
    return {
        "view": "list",
        "region_url": "/settings/contacts",
        "has_owner": any(contact.role is ContactRole.owner for contact in contacts),
        "add_owner_name_key": uuid4().hex,
        "rows": _rows(db, contacts),
        "create_key": uuid4().hex,
    }


def detail_context(db: Session, contact_id: int) -> dict[str, object]:
    """The page of one contact, yours included. Raises LookupError for no such contact."""
    contacts = load_contacts(db)
    contact = next((c for c in contacts if c.id == contact_id), None)
    if contact is None:
        raise LookupError(f"no contact {contact_id}")
    is_owner = contact.role is ContactRole.owner
    balance = None
    if contact.role is ContactRole.other:
        balance = _balance_view(db, contact_balance(db, contact_id, joint_share=joint_share_on(db)))
    detail = ContactDetail(
        editor=_editor(contact, _name_matches(db, contact) if is_owner else None),
        kind=KIND_LABELS[contact.kind],
        kind_value=contact.kind.value,
        role=ROLE_LABELS[contact.role],
        role_value=contact.role.value,
        is_owner=is_owner,
        balance=balance,
        merge_choices=tuple(
            (other.id, other.name)
            for other in sorted(contacts, key=lambda c: c.name.casefold())
            if other.id != contact_id
            and other.role is not ContactRole.owner
            and (contact.role is ContactRole.other or other.role is contact.role)
        ),
        edit_key=uuid4().hex,
        role_key=uuid4().hex,
        delete_key=uuid4().hex,
        merge_key=uuid4().hex,
    )
    return {"view": "detail", "region_url": f"/settings/contacts/{contact_id}", "detail": detail}


def region_context(db: Session, contact_id: int | None) -> dict[str, object]:
    """The view a write leaves on screen: the contact's page, or the list when there is no contact
    or it is gone."""
    if contact_id is not None:
        try:
            return detail_context(db, contact_id)
        except LookupError:
            pass
    return list_context(db)


# Pickers


def other_side_choices(db: Session, *, with_owner_accounts: bool = True) -> tuple[ChoiceGroup, ...]:
    """Everything an other side can be set to: each contact and each of its accounts, and the
    owner's accounts, which are the user's accounts outside the ledger."""
    contacts = load_contacts(db)
    groups: list[ChoiceGroup] = []
    owner = next((c for c in contacts if c.role is ContactRole.owner), None)
    if owner is not None and owner.accounts and with_owner_accounts:
        groups.append(
            ChoiceGroup(
                "Your accounts",
                tuple(
                    Choice(f"{owner.id}:{account.id}", _account_text(account))
                    for account in owner.accounts
                ),
            )
        )
    others = sorted(
        (c for c in contacts if c.role is not ContactRole.owner), key=lambda c: c.name.casefold()
    )
    if others:
        choices: list[Choice] = []
        for contact in others:
            suffix = " (co-owner)" if contact.role is ContactRole.co_owner else ""
            choices.append(Choice(str(contact.id), f"{contact.name}{suffix}"))
            choices.extend(
                Choice(f"{contact.id}:{account.id}", f"{contact.name} · {_account_text(account)}")
                for account in contact.accounts
            )
        groups.append(ChoiceGroup("Contacts", tuple(choices)))
    return tuple(groups)


def parse_choice(value: str) -> tuple[int, int | None]:
    """The contact and optional account a picker value names. Raises ValueError otherwise."""
    match = re.fullmatch(r"(\d+)(?::(\d+))?", value.strip())
    if match is None:
        raise ValueError("Choose who is on the other side.")
    contact_id, account_id = match.groups()
    return int(contact_id), int(account_id) if account_id else None


def selected_choice(contact_id: int | None, account_id: int | None) -> str:
    if contact_id is None:
        return ""
    return f"{contact_id}:{account_id}" if account_id is not None else str(contact_id)


# Builders


def _editor(contact: ContactView, matches: dict[int, int] | None = None) -> ContactEditor:
    """The editors of one contact. matches counts, by name ID, the transactions each of your names
    matches; it is None for a contact that is not you."""
    tied: dict[int, list[PatternRow]] = {}
    loose: list[PatternRow] = []
    for pattern in contact.patterns:
        row = PatternRow(pattern.id, pattern.side, pattern.pattern, uuid4().hex)
        if pattern.contact_account_id is None:
            loose.append(row)
        else:
            tied.setdefault(pattern.contact_account_id, []).append(row)
    return ContactEditor(
        id=contact.id,
        name=contact.name,
        is_owner=contact.role is ContactRole.owner,
        names=tuple(
            NameRow(
                id=name.id or 0,
                text=name.text,
                forms=", ".join(sorted(name.forms)),
                matches=None if matches is None else matches.get(name.id or 0, 0),
                remove_key=uuid4().hex,
            )
            for name in contact.names
        ),
        accounts=tuple(
            AccountRow(
                id=account.id,
                label=account.label,
                digits=f"••{account.last4}" if account.last4 else "",
                patterns=tuple(tied.get(account.id, ())),
                remove_key=uuid4().hex,
                add_pattern_key=uuid4().hex,
            )
            for account in contact.accounts
        ),
        loose_patterns=tuple(loose),
        account_choices=tuple((account.id, account.label) for account in contact.accounts),
        add_name_key=uuid4().hex,
        add_account_key=uuid4().hex,
        add_pattern_key=uuid4().hex,
    )


def _rows(db: Session, contacts: list[ContactView]) -> tuple[ContactRow, ...]:
    """You first, then the contacts with a balance, largest first, then the rest by name."""
    balances = contact_balances(db, joint_share=joint_share_on(db))
    rows: list[ContactRow] = [_plain_row(c) for c in contacts if c.role is ContactRole.owner]
    rows.extend(
        ContactRow(
            id=balance.contact_id,
            name=balance.name,
            kind=KIND_LABELS[next(c.kind for c in contacts if c.id == balance.contact_id)],
            role="",
            balance_cents=balance.balance_cents,
            direction=DIRECTION_LABELS[balance.direction],
            open_items=len(balance.open_items),
        )
        for balance in balances
    )
    listed = {row.id for row in rows}
    rest = sorted(
        (c for c in contacts if c.role is not ContactRole.owner and c.id not in listed),
        key=lambda c: (c.name.casefold(), c.id),
    )
    rows.extend(_plain_row(contact) for contact in rest)
    return tuple(rows)


def _plain_row(contact: ContactView) -> ContactRow:
    """A row with no balance line: you and a co-owner show no balance, and a contact whose lines
    all cleared shows settled."""
    keeps_balance = contact.role is ContactRole.other
    return ContactRow(
        id=contact.id,
        name=contact.name,
        kind=KIND_LABELS[contact.kind],
        role=ROLE_LABELS[contact.role],
        balance_cents=0 if keeps_balance else None,
        direction=DIRECTION_LABELS[Direction.settled] if keeps_balance else "",
        open_items=0,
    )


def _balance_view(db: Session, balance: ContactBalance) -> BalanceView:
    ids = {item.transaction_id for item in balance.open_items} | {
        line.transaction_id for line in balance.lines
    }
    payees = {
        txn_id: payee
        for txn_id, payee in db.execute(
            select(Transaction.id, Transaction.payee).where(Transaction.id.in_(ids))
        )
    }
    return BalanceView(
        cents=balance.balance_cents,
        direction=DIRECTION_LABELS[balance.direction],
        direction_key=balance.direction.value,
        open_items=tuple(
            OpenItemRow(
                txn_id=item.transaction_id,
                on=_day(item.posted_on),
                payee=payees.get(item.transaction_id, ""),
                amount_cents=item.amount_cents,
                open_cents=item.open_cents,
            )
            for item in balance.open_items
        ),
        lines=tuple(
            LineRow(
                txn_id=line.transaction_id,
                on=_day(line.posted_on),
                payee=payees.get(line.transaction_id, ""),
                amount_cents=line.amount_cents,
                cleared_cents=line.cleared_cents,
                is_split=line.split_id is not None,
            )
            for line in balance.lines
        ),
    )


def _name_matches(db: Session, owner: ContactView) -> dict[int, int]:
    """How many transactions each of your names matches in payee text, raw or display, by name ID.
    One pass over the payees runs every name's pattern."""
    patterns: list[tuple[int, re.Pattern[str]]] = []
    for name in owner.names:
        pattern = names_pattern(name_literals(name))
        if pattern is not None:
            patterns.append((name.id or 0, pattern))
    counts = dict.fromkeys((name_id for name_id, _ in patterns), 0)
    if not patterns:
        return counts
    payees = db.execute(
        select(Transaction.payee_raw, Transaction.payee).execution_options(yield_per=500)
    )
    for payee_raw, payee in payees:
        for name_id, pattern in patterns:
            if pattern.search(payee_raw) or pattern.search(payee):
                counts[name_id] += 1
    return counts


def _account_text(account: ContactAccountView) -> str:
    return f"{account.label} ••{account.last4}" if account.last4 else account.label


def _day(day: date) -> str:
    return day.isoformat()
