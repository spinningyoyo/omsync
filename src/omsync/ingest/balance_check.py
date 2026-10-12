"""Balance check: does each statement balance equal the one before it plus the transactions between?

A mismatch means missing or duplicate transactions, so the result is shown on the account.
The check only reads. It never writes a balance or a transaction.

Only ledger balances are checked. Available balances are ignored. For each consecutive pair of
balance dates (prev, next), the transactions counted are those with prev < posted_on <= next.
A transaction on prev is already in prev's balance, and one on next is in next's. Parent
transaction amounts are used, so splits never change the result. All money is integer cents.

Two ledger balances on one date that agree are one balance, such as the same statement
imported twice. Two that disagree are an issue of their own, because an institution reports one
balance per moment.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.ledger.sources import Account
from omsync.ledger.statements import BalanceKind, StatementBalance
from omsync.ledger.transactions import Transaction


@dataclass(frozen=True)
class BalanceIssue:
    """One place the balances do not add up.

    For a gap between two dates, expected is the later statement balance and computed is the
    earlier balance plus the transactions between them. For two balances on one date,
    expected is the first one recorded, computed is the one that disagrees with it, and
    from_date and to_date are the same day. difference is always expected minus computed.
    """

    from_date: date
    to_date: date
    expected_cents: int
    computed_cents: int
    difference_cents: int


@dataclass(frozen=True)
class BalanceStatus:
    """The balance check for one account.

    checked_through is the latest balance date that agrees with the balance before it. A
    single balance agrees with itself. A date with conflicting balances never agrees. It is
    None when no balance agrees. balances_seen counts distinct (date, balance) pairs.
    """

    account_id: int
    ok: bool
    issues: tuple[BalanceIssue, ...]
    checked_through: date | None
    balances_seen: int


def check_account(session: Session, account_id: int) -> BalanceStatus:
    """Check one account's ledger balances in date order. Read-only."""
    rows = session.execute(
        select(StatementBalance.id, StatementBalance.as_of, StatementBalance.balance_cents)
        .where(
            StatementBalance.account_id == account_id,
            StatementBalance.kind == BalanceKind.ledger,
        )
        .order_by(StatementBalance.id)
    ).all()

    values_by_date: dict[date, list[int]] = {}
    for _id, as_of, cents in rows:
        values = values_by_date.setdefault(as_of, [])
        if cents not in values:
            values.append(cents)
    balances_seen = sum(len(values) for values in values_by_date.values())

    dates = sorted(values_by_date)
    days, totals = _daily_totals(session, account_id)
    issues: list[BalanceIssue] = []
    checked_through: date | None = None
    previous: tuple[date, int] | None = None
    for as_of in dates:
        first, *others = values_by_date[as_of]
        agrees = not others
        for other in others:
            issues.append(
                BalanceIssue(
                    from_date=as_of,
                    to_date=as_of,
                    expected_cents=first,
                    computed_cents=other,
                    difference_cents=first - other,
                )
            )

        if previous is None:
            # Nothing comes before the first balance, so it is checked only when it is alone.
            if len(dates) == 1 and agrees:
                checked_through = as_of
        else:
            prev_date, prev_cents = previous
            computed = prev_cents + _between(days, totals, prev_date, as_of)
            difference = first - computed
            if difference != 0:
                issues.append(
                    BalanceIssue(
                        from_date=prev_date,
                        to_date=as_of,
                        expected_cents=first,
                        computed_cents=computed,
                        difference_cents=difference,
                    )
                )
            if agrees and difference == 0:
                checked_through = as_of
        previous = (as_of, first)

    return BalanceStatus(
        account_id=account_id,
        ok=not issues,
        issues=tuple(issues),
        checked_through=checked_through,
        balances_seen=balances_seen,
    )


def check_all(session: Session) -> list[BalanceStatus]:
    """Check every account that is not closed, in account ID order. Read-only."""
    account_ids = session.scalars(
        select(Account.id).where(Account.closed.is_(False)).order_by(Account.id)
    ).all()
    return [check_account(session, account_id) for account_id in account_ids]


def _daily_totals(session: Session, account_id: int) -> tuple[list[date], list[int]]:
    """The account's parent transaction amounts summed per posting date, one query for the account.

    Returns the dates in order and running totals: totals[i] is the sum over the first i dates.
    """
    rows = session.execute(
        select(Transaction.posted_on, func.sum(Transaction.amount_cents))
        .where(Transaction.account_id == account_id)
        .group_by(Transaction.posted_on)
        .order_by(Transaction.posted_on)
    ).all()
    days = [row[0] for row in rows]
    totals = [0]
    for _day, cents in rows:
        totals.append(totals[-1] + int(cents))
    return days, totals


def _between(days: list[date], totals: list[int], after: date, through: date) -> int:
    """The sum of the amounts posted after `after` and on or before `through`."""
    low = bisect_right(days, after)
    high = bisect_right(days, through)
    return totals[high] - totals[low] if high > low else 0
