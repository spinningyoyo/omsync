"""Queries and account edits for the accounts panel and the account page.

Reads are plain queries. Balances are the ledger balances from statements, one point per date,
and the latest one is the account's balance. update_account is one batch, so it can be undone
like every other bulk change, and so is add_account_number, which maps a reissued card's new last
four digits to the account. This module never imports omsync.web.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import Change, applied_batch_for_key, apply, make_preview, snapshot
from omsync.ledger.accounts import add_alias, find_account
from omsync.ledger.models import (
    Account,
    AccountAlias,
    AccountType,
    BalanceKind,
    Institution,
    Provider,
    StatementBalance,
    StatementFile,
    StatementRow,
    Transaction,
)
from omsync.ledger.sources import current_alias_id

TREND_DAYS = 30
NAME_MAX = 60
LAST4_LENGTH = 4
TYPE_ORDER: tuple[AccountType, ...] = (
    AccountType.checking,
    AccountType.savings,
    AccountType.credit_card,
    AccountType.brokerage,
    AccountType.other,
)


@dataclass(frozen=True)
class BalancePoint:
    as_of: date
    cents: int


@dataclass(frozen=True)
class AccountSummary:
    """One account as the panel and the page header show it.

    latest is None until a ledger balance has been imported. recent holds the balance points of
    the TREND_DAYS days up to latest, oldest first, led by the balance carried into those days
    when there is one, so the trend starts from what the account held then. Credit card balances
    are negative when you owe, as the statement reports them.
    """

    id: int
    name: str
    institution_id: int
    institution_key: str
    institution: str
    last4: str | None
    alias_last4: str | None
    type: AccountType
    joint: bool
    closed: bool
    latest: BalancePoint | None
    recent: tuple[BalancePoint, ...]


@dataclass(frozen=True)
class TypeTotal:
    """The sum of the latest balances of one type. total_cents is None when none has a balance."""

    type: AccountType
    total_cents: int | None
    count: int


@dataclass(frozen=True)
class AccountNumber:
    """One number the bank and its statements know an account by, as its last four digits.

    latest is the last date its statements reach, None while no statement carries it. current marks
    the one sync looks for and screens show (see ledger.sources.current_alias_id).
    """

    last4: str
    latest: date | None
    current: bool


@dataclass(frozen=True)
class AccountDetail:
    summary: AccountSummary
    institution_key: str
    points: tuple[BalancePoint, ...]  # every ledger balance point, oldest first
    connector_key: str | None  # the institution's first provider's connector; None without one
    connector_enabled: bool
    transaction_count: int
    first_posted: date | None
    last_posted: date | None


def account_summaries(session: Session) -> list[AccountSummary]:
    """Every open account, ordered by type, then name. Read-only."""
    rows = session.execute(
        select(Account, Institution)
        .join(Institution, Institution.id == Account.institution_id)
        .where(Account.closed.is_(False))
    ).all()
    points = _balance_points(session)
    summaries = [
        _summary(session, account, institution, points.get(account.id, []))
        for account, institution in rows
    ]
    return sorted(summaries, key=lambda s: (TYPE_ORDER.index(s.type), s.name.casefold(), s.id))


def closed_accounts(session: Session) -> list[Account]:
    """Every closed account, ordered by name. Read-only."""
    return list(
        session.scalars(
            select(Account).where(Account.closed.is_(True)).order_by(Account.name, Account.id)
        )
    )


def account_summary(session: Session, account_id: int) -> AccountSummary:
    """One account, open or closed. Raises LookupError if there is no such account."""
    row = session.execute(
        select(Account, Institution)
        .join(Institution, Institution.id == Account.institution_id)
        .where(Account.id == account_id)
    ).first()
    if row is None:
        raise LookupError(f"There is no account {account_id}.")
    account, institution = row
    points = _balance_points(session, [account_id]).get(account_id, [])
    return _summary(session, account, institution, points)


def group_totals(summaries: Sequence[AccountSummary]) -> list[TypeTotal]:
    """The latest balances summed per type, in TYPE_ORDER, for the types that have accounts.

    Balances are summed as stated, so a credit card you owe on is negative in its total.
    """
    totals: list[TypeTotal] = []
    for account_type in TYPE_ORDER:
        members = [summary for summary in summaries if summary.type == account_type]
        if not members:
            continue
        balances = [s.latest.cents for s in members if s.latest is not None]
        totals.append(
            TypeTotal(
                type=account_type,
                total_cents=sum(balances) if balances else None,
                count=len(members),
            )
        )
    return totals


def account_detail(session: Session, account_id: int) -> AccountDetail:
    """An account, open or closed, with everything its page shows. Read-only.

    Raises LookupError if there is no such account.
    """
    summary = account_summary(session, account_id)
    account = session.get(Account, account_id)
    assert account is not None  # account_summary just found it
    institution = session.get(Institution, account.institution_id)
    assert institution is not None  # every account belongs to an institution

    provider = session.scalars(
        select(Provider).where(Provider.institution_id == institution.id).order_by(Provider.id)
    ).first()
    count, first, last = session.execute(
        select(
            func.count(Transaction.id),
            func.min(Transaction.posted_on),
            func.max(Transaction.posted_on),
        ).where(Transaction.account_id == account_id)
    ).one()

    return AccountDetail(
        summary=summary,
        institution_key=institution.key,
        points=tuple(_balance_points(session, [account_id]).get(account_id, [])),
        connector_key=None if provider is None else provider.connector_key,
        connector_enabled=bool(provider is not None and provider.enabled),
        transaction_count=int(count),
        first_posted=first,
        last_posted=last,
    )


def update_account(
    session: Session,
    account_id: int,
    *,
    name: str | None = None,
    joint: bool | None = None,
    closed: bool | None = None,
    last4: str | None = None,
    idempotency_key: str,
) -> Batch:
    """Change an account's name, joint flag, closed flag, or displayed last four as one batch.

    None leaves a field alone, and a value equal to the account's own is not a change. Raises
    LookupError if there is no such account, and ValueError for a blank or too-long name, a last
    four that is not four characters, or when nothing would change. A stale change raises
    BatchError from apply(). The same key applies at most once.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = snapshot(session, "account", account_id)
    if before is None:
        raise LookupError(f"There is no account {account_id}.")
    after = dict(before)
    if name is not None:
        after["name"] = _clean_name(name)
    if joint is not None:
        after["joint"] = bool(joint)
    if closed is not None:
        after["closed"] = bool(closed)
    if last4 is not None:
        after["last4"] = _clean_last4(last4)
    if after == before:
        raise ValueError("Nothing has changed.")
    change = Change("account", str(account_id), before, after)
    preview = make_preview(
        session,
        "account.update",
        [change],
        summary={"label": _label(before, after), "account_id": account_id},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def account_numbers(session: Session, account_id: int) -> tuple[AccountNumber, ...]:
    """The account's numbers by their last four digits, the current one first, then the latest
    statements first. Aliases shorter than four characters are left out. Read-only."""
    aliases = session.scalars(
        select(AccountAlias).where(
            AccountAlias.account_id == account_id,
            func.length(AccountAlias.statement_account_id) >= LAST4_LENGTH,
        )
    ).all()
    current_id = session.scalar(select(current_alias_id(account_id)))
    file_date = func.coalesce(
        StatementFile.period_end,
        select(func.max(StatementRow.posted_on))
        .where(StatementRow.statement_file_id == StatementFile.id)
        .scalar_subquery(),
    )
    digits = func.substr(StatementFile.statement_account_id, -LAST4_LENGTH)
    latest: dict[str, date | None] = dict(
        session.execute(
            select(digits, func.max(file_date))
            .where(StatementFile.account_id == account_id)
            .group_by(digits)
        ).all()
    )
    numbers: dict[str, AccountNumber] = {}
    for alias in aliases:
        last4 = alias.statement_account_id[-LAST4_LENGTH:]
        current = alias.id == current_id or (last4 in numbers and numbers[last4].current)
        numbers[last4] = AccountNumber(last4, latest.get(last4), current)
    return tuple(
        sorted(
            numbers.values(),
            key=lambda n: (not n.current, n.latest is None, -(n.latest or date.min).toordinal()),
        )
    )


def add_account_number(
    session: Session, account_id: int, last4: str, *, idempotency_key: str
) -> Batch:
    """Map a reissued card's new last four digits to the account, as one alias batch. With no
    statement carrying them yet, they become the number sync looks for, and the first file sync
    fetches for them is linked to the account (see ingest.sync).

    Raises LookupError if there is no such account, and ValueError for anything but four digits or
    digits the account or another account at its institution already has. The same key applies at
    most once."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    row = session.execute(
        select(Account, Institution.key)
        .join(Institution, Institution.id == Account.institution_id)
        .where(Account.id == account_id)
    ).first()
    if row is None:
        raise LookupError(f"There is no account {account_id}.")
    account, institution_key = row
    clean = last4.strip()
    if len(clean) != LAST4_LENGTH or not clean.isdigit():
        raise ValueError(f"Give the {LAST4_LENGTH} digits the bank shows for the new card.")
    if any(number.last4 == clean for number in account_numbers(session, account_id)):
        raise ValueError(f"••{clean} is already one of this account's numbers.")
    if find_account(session, institution_key, clean) is not None:
        raise ValueError(f"••{clean} already belongs to another account.")
    return add_alias(
        session,
        account_id=account.id,
        institution_key=institution_key,
        statement_account_id=clean,
        idempotency_key=idempotency_key,
    )


def _label(before: dict[str, object], after: dict[str, object]) -> str:
    """The undo toast's words for an edit. It names the first thing that changed."""
    name = str(after["name"])
    if after["closed"] != before["closed"]:
        return f"Closed {name}" if after["closed"] else f"Reopened {name}"
    if after["name"] != before["name"]:
        return f"Renamed to {name}"
    if after["last4"] != before["last4"]:
        return f"Changed {name} to ••{after['last4']}"
    return f"Marked {name} joint" if after["joint"] else f"Marked {name} not joint"


def _clean_last4(last4: str) -> str:
    clean = last4.strip()
    if len(clean) != LAST4_LENGTH:
        raise ValueError(f"Give exactly {LAST4_LENGTH} characters for the last digits.")
    return clean


def _clean_name(name: str) -> str:
    clean = name.strip()
    if not clean:
        raise ValueError("Give the account a name.")
    if len(clean) > NAME_MAX:
        raise ValueError(f"Names can be up to {NAME_MAX} characters.")
    return clean


def _summary(
    session: Session,
    account: Account,
    institution: Institution,
    points: list[BalancePoint],
) -> AccountSummary:
    return AccountSummary(
        id=account.id,
        name=account.name,
        institution_id=account.institution_id,
        institution_key=institution.key,
        institution=institution.name,
        last4=account.last4,
        alias_last4=account.alias_last4,
        type=account.type,
        joint=bool(account.joint),
        closed=bool(account.closed),
        latest=points[-1] if points else None,
        recent=_trend(points),
    )


def _trend(points: list[BalancePoint]) -> tuple[BalancePoint, ...]:
    """The points of the TREND_DAYS days up to the latest, with the last one before them."""
    if not points:
        return ()
    start = points[-1].as_of - timedelta(days=TREND_DAYS)
    first = next(i for i, point in enumerate(points) if point.as_of >= start)
    return tuple(points[max(first - 1, 0) :])


def _balance_points(
    session: Session, account_ids: Sequence[int] | None = None
) -> dict[int, list[BalancePoint]]:
    """The ledger balance points of each account, oldest first, one per date.

    When two ledger balances share a date, the one imported last (highest id) is used, so the
    latest balance matches the one the panel has always shown.
    """
    stmt = (
        select(StatementBalance.account_id, StatementBalance.as_of, StatementBalance.balance_cents)
        .where(StatementBalance.kind == BalanceKind.ledger)
        .order_by(StatementBalance.account_id, StatementBalance.as_of, StatementBalance.id)
    )
    if account_ids is not None:
        stmt = stmt.where(StatementBalance.account_id.in_(list(account_ids)))
    by_day: dict[int, dict[date, int]] = {}
    for account_id, as_of, cents in session.execute(stmt):
        by_day.setdefault(account_id, {})[as_of] = cents
    return {
        account_id: [BalancePoint(as_of=day, cents=cents) for day, cents in sorted(days.items())]
        for account_id, days in by_day.items()
    }
