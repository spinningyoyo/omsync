"""Queries and account edits for the accounts panel and the account page.

Reads are plain queries. Balances are the ledger balances from statements, one point per date,
and the latest one is the account's balance. update_account is one batch, so it can be undone
like every other bulk change. This module never imports omsync.web.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import Change, applied_batch_for_key, apply, make_preview, snapshot
from omsync.ingest.balance_check import BalanceStatus, check_account
from omsync.ledger.models import (
    Account,
    AccountType,
    BalanceKind,
    Institution,
    Provider,
    StatementBalance,
    SyncRun,
    SyncStatus,
    Transaction,
)

SPARKLINE_POINTS = 30
RECENT_SYNCS = 5
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

    latest is None until a ledger balance has been imported. recent holds the last
    SPARKLINE_POINTS balance points, oldest first. Credit card balances are negative when you
    owe, as the statement reports them.
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
    status: BalanceStatus


@dataclass(frozen=True)
class TypeTotal:
    """The sum of the latest balances of one type. total_cents is None when none has a balance."""

    type: AccountType
    total_cents: int | None
    count: int


@dataclass(frozen=True)
class RecentSync:
    """One SyncRun, as the account page's sync card shows it. message is already safe to show."""

    id: int
    status: SyncStatus
    started_at: datetime  # naive UTC, as SyncRun stores it; the web layer converts it for display
    finished_at: datetime | None
    files_fetched: int
    message: str | None


@dataclass(frozen=True)
class AccountDetail:
    summary: AccountSummary
    institution_key: str
    points: tuple[BalancePoint, ...]  # every ledger balance point, oldest first
    connector_key: str | None  # the institution's first provider's connector; None without one
    connector_enabled: bool
    recent_syncs: tuple[RecentSync, ...]  # the last RECENT_SYNCS runs, newest first
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
    runs = session.scalars(
        select(SyncRun)
        .where(SyncRun.institution_key == institution.key)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(RECENT_SYNCS)
    ).all()
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
        recent_syncs=tuple(
            RecentSync(
                id=run.id,
                status=run.status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                files_fetched=run.files_fetched,
                message=run.message,
            )
            for run in runs
        ),
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
        recent=tuple(points[-SPARKLINE_POINTS:]),
        status=check_account(session, account.id),
    )


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
