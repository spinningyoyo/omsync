"""Contact balances: what is owed between you and each contact, and which money it is owed for.

Every number is integer cents, and these are reads: nothing here writes.

A line is money that moves what one side owes the other. A contact's lines are its transactions
without splits, and its splits, whose category type is owed, such as a loan. Transfers are between
your own accounts, so they are not lines. A split belongs to the contact it names, or, naming none,
to the contact of its transaction. Spending and income lines are settled when paid, so they never
count. A line counts as reports count it, joint
share included (see ledger/spending.py), except that a link never reduces it: the two payments of
a round trip cancel by clearing.

Money out to a contact (a negative amount) is what the contact owes you, and money in from them
reduces it. The balance is the negated sum of the counted amounts: positive when the contact owes
you, negative when you hold money for the contact or owe it.

Lines clear oldest first, by date, then transaction, then split position. Each line first clears
the open items of the opposite direction, oldest first and partly when the amounts differ, and any
remainder opens as an item of its own. So at most one direction has open items, and they add up to
the balance. No pairing rule, window, or guess is involved.

The owner and a co-owner have no balance: the owner's lines are your own accounts, and the owners
keep none between them. This module never imports omsync.web.
"""

from collections import defaultdict, deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from omsync.ledger.contacts import Contact, ContactRole
from omsync.ledger.models import Account, Category, CategoryType, Split, Transaction
from omsync.ledger.spending import counted_amount, counted_splits


class NoBalance(ValueError):
    """The contact is the owner or a co-owner, which have no balance."""


class Direction(StrEnum):
    """Who owes whom. you_owe also means you hold money for the contact."""

    owes_you = "owes_you"
    you_owe = "you_owe"
    settled = "settled"


@dataclass(frozen=True)
class BalanceLine:
    """One counted line. amount_cents keeps its sign, and cleared_cents is how much of its
    magnitude cleared against lines of the opposite direction."""

    transaction_id: int
    split_id: int | None
    posted_on: date
    amount_cents: int
    cleared_cents: int


@dataclass(frozen=True)
class OpenItem:
    """A line, or the part of it, that nothing has cleared. open_cents is a magnitude: the
    direction is the balance's."""

    transaction_id: int
    split_id: int | None
    posted_on: date
    amount_cents: int
    open_cents: int


@dataclass(frozen=True)
class ContactBalance:
    contact_id: int
    name: str
    balance_cents: int
    direction: Direction
    open_items: tuple[OpenItem, ...]  # oldest first; open_cents adds up to abs(balance_cents)
    lines: tuple[BalanceLine, ...]  # oldest first


@dataclass(frozen=True)
class _Line:
    contact_id: int
    transaction_id: int
    split_id: int | None
    position: int
    posted_on: date
    amount_cents: int


# Public queries


def contact_balance(session: Session, contact_id: int, *, joint_share: bool) -> ContactBalance:
    """The balance with one contact. Raises LookupError for an unknown contact and NoBalance for
    the owner or a co-owner."""
    contact = session.get(Contact, contact_id)
    if contact is None:
        raise LookupError(f"no contact {contact_id}")
    if contact.role is not ContactRole.other:
        raise NoBalance(
            f"{contact.name} is {contact.role.value.replace('_', '-')}, so has no balance"
        )
    lines = _lines(session, {contact_id}, joint_share=joint_share)
    return _balance(contact_id, contact.name, lines.get(contact_id, []))


def contact_balances(session: Session, *, joint_share: bool) -> list[ContactBalance]:
    """Every contact that owes you something or is owed something, largest balance first and by
    name among equals. A contact whose lines all cleared has no balance and is left out."""
    others = {
        contact.id: contact.name
        for contact in session.scalars(select(Contact).where(Contact.role == ContactRole.other))
    }
    lines = _lines(session, others.keys(), joint_share=joint_share)
    balances = [
        _balance(contact_id, others[contact_id], found) for contact_id, found in lines.items()
    ]
    return sorted(
        (balance for balance in balances if balance.balance_cents != 0),
        key=lambda balance: (
            -abs(balance.balance_cents),
            balance.name.casefold(),
            balance.contact_id,
        ),
    )


# Lines


def _lines(
    session: Session, contact_ids: Collection[int], *, joint_share: bool
) -> dict[int, list[_Line]]:
    """The counted lines of each contact in contact_ids that has any, oldest first."""
    if not contact_ids:
        return {}
    on_a_split = select(Split.transaction_id).where(Split.contact_id.in_(contact_ids))
    transactions = session.scalars(
        select(Transaction)
        .options(selectinload(Transaction.splits))
        .where(or_(Transaction.contact_id.in_(contact_ids), Transaction.id.in_(on_a_split)))
    ).all()
    joint = set(session.scalars(select(Account.id).where(Account.joint)))
    owed = set(session.scalars(select(Category.id).where(Category.type == CategoryType.owed)))

    found: dict[int, list[_Line]] = defaultdict(list)
    for txn in transactions:
        # No links reduce a line here, so nothing is covered.
        counted = counted_amount(txn.amount_cents, 0, joint_share and txn.account_id in joint)
        if not txn.splits:
            if txn.contact_id in contact_ids and txn.category_id in owed and counted:
                found[txn.contact_id].append(
                    _Line(txn.contact_id, txn.id, None, 0, txn.posted_on, counted)
                )
            continue
        shares = counted_splits(
            txn.amount_cents, [part.amount_cents for part in txn.splits], counted
        )
        for part, share in zip(txn.splits, shares, strict=True):
            owner = part.contact_id if part.contact_id is not None else txn.contact_id
            if owner in contact_ids and part.category_id in owed and share:
                found[owner].append(
                    _Line(owner, txn.id, part.id, part.position, txn.posted_on, share)
                )
    return {
        contact_id: sorted(
            lines, key=lambda line: (line.posted_on, line.transaction_id, line.position)
        )
        for contact_id, lines in found.items()
    }


# Clearing


def _balance(contact_id: int, name: str, lines: Sequence[_Line]) -> ContactBalance:
    """Clear the lines, which are in order, and read the balance and open items off what is left."""
    remaining = [abs(line.amount_cents) for line in lines]
    owed_to_you: deque[int] = deque()  # indexes of money out with something left
    owed_by_you: deque[int] = deque()  # indexes of money in with something left
    for index, line in enumerate(lines):
        same, opposite = (
            (owed_to_you, owed_by_you) if line.amount_cents < 0 else (owed_by_you, owed_to_you)
        )
        while remaining[index] and opposite:
            older = opposite[0]
            cleared = min(remaining[index], remaining[older])
            remaining[index] -= cleared
            remaining[older] -= cleared
            if remaining[older] == 0:
                opposite.popleft()
        if remaining[index]:
            same.append(index)

    balance = -sum(line.amount_cents for line in lines)
    still_open = owed_to_you or owed_by_you
    return ContactBalance(
        contact_id=contact_id,
        name=name,
        balance_cents=balance,
        direction=_direction(balance),
        open_items=tuple(
            OpenItem(
                transaction_id=lines[index].transaction_id,
                split_id=lines[index].split_id,
                posted_on=lines[index].posted_on,
                amount_cents=lines[index].amount_cents,
                open_cents=remaining[index],
            )
            for index in still_open
        ),
        lines=tuple(
            BalanceLine(
                transaction_id=line.transaction_id,
                split_id=line.split_id,
                posted_on=line.posted_on,
                amount_cents=line.amount_cents,
                cleared_cents=abs(line.amount_cents) - remaining[index],
            )
            for index, line in enumerate(lines)
        ),
    )


def _direction(balance: int) -> Direction:
    if balance > 0:
        return Direction.owes_you
    return Direction.you_owe if balance < 0 else Direction.settled


__all__ = [
    "BalanceLine",
    "ContactBalance",
    "Direction",
    "NoBalance",
    "OpenItem",
    "contact_balance",
    "contact_balances",
]
