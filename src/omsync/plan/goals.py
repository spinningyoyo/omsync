"""Goal changes and progress projections from reported account balances."""

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum, StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import Change, applied_batch_for_key, apply, make_preview, snapshot
from omsync.core.months import shift_months
from omsync.ledger.models import Account, BalanceKind, Goal, StatementBalance

BASIS_POINTS = 10_000
ICON_MAX = 32
COLOR_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}")
ICON_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


class GoalStatus(StrEnum):
    complete = "complete"  # nothing remains
    on_track = "on_track"  # no target date, or the projected date is on or before it
    behind = "behind"  # the projected date is after the target date, or never comes


class _Unset(Enum):
    """The type of UNSET, the default for update_goal's fields. It means: leave the field alone.
    That leaves None free to mean: clear a field that can be cleared."""

    UNSET = "unset"


UNSET = _Unset.UNSET


@dataclass(frozen=True)
class GoalProgress:
    """A goal's progress on one day. `saved_cents` is the linked account's latest ledger balance,
    clamped at 0, or 0 with no linked account or no ledger balance. `balance_as_of` is the date of
    that balance, or None when saved is 0 for lack of one.

    `percent_bp` is saved over target in floored basis points, capped at 10000.
    `projected_date` is today plus ceil(remaining / contribution) months, or None when nothing is
    contributed and something remains, or when that date would pass year 9999.
    `required_monthly_cents` is the contribution that reaches the target by the target date, or
    None without a target date.
    """

    goal_id: int
    target_cents: int
    saved_cents: int
    remaining_cents: int
    percent_bp: int
    monthly_contribution_cents: int
    projected_date: date | None
    required_monthly_cents: int | None
    status: GoalStatus
    balance_as_of: date | None


@dataclass(frozen=True)
class GoalsSummary:
    """Each listed goal with its progress, then totals over those goals and a count per status.
    The totals and counts cover exactly the listed goals, so archived ones are in them only when
    they are listed."""

    rows: tuple[tuple[Goal, GoalProgress], ...]
    target_cents: int
    saved_cents: int
    monthly_contribution_cents: int
    status_counts: dict[GoalStatus, int]


# Writes. Each one is a batch, and a repeated idempotency key returns the first batch.


def create_goal(
    session: Session,
    *,
    name: str,
    target_cents: int,
    target_date: date | None = None,
    linked_account_id: int | None = None,
    monthly_contribution_cents: int = 0,
    color: str | None = None,
    icon: str | None = None,
    idempotency_key: str,
) -> Batch:
    """Add a goal as one batch. Raises ValueError for a field that breaks its rule, and writes
    nothing. The same key applies at most once."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    fields = _clean(
        session,
        {
            "name": name,
            "target_cents": target_cents,
            "target_date": target_date,
            "linked_account_id": linked_account_id,
            "monthly_contribution_cents": monthly_contribution_cents,
            "color": color,
            "icon": icon,
        },
        check_account=True,
    )
    change = Change("goal", None, None, {**fields, "archived": False})
    preview = make_preview(
        session,
        "goal.create",
        [change],
        summary={"label": f"Added {fields['name']}", "name": fields["name"]},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def update_goal(
    session: Session,
    goal_id: int,
    *,
    name: str | _Unset = UNSET,
    target_cents: int | _Unset = UNSET,
    target_date: date | None | _Unset = UNSET,
    linked_account_id: int | None | _Unset = UNSET,
    monthly_contribution_cents: int | _Unset = UNSET,
    color: str | None | _Unset = UNSET,
    icon: str | None | _Unset = UNSET,
    idempotency_key: str,
) -> Batch:
    """Change the given fields as one batch. Omit a field to keep it, and pass None to clear one
    that can be cleared. Raises LookupError if there is no such goal, and ValueError when nothing
    would change or a field breaks its rule.

    A goal keeps its link to an account that is closed afterward. The account is checked only when
    the link is being set to a different account, so renaming such a goal still works.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = snapshot(session, "goal", goal_id)
    if before is None:
        raise LookupError(f"There is no goal {goal_id}.")
    current = _editable(session.get(Goal, goal_id))
    given = {
        key: value
        for key, value in {
            "name": name,
            "target_cents": target_cents,
            "target_date": target_date,
            "linked_account_id": linked_account_id,
            "monthly_contribution_cents": monthly_contribution_cents,
            "color": color,
            "icon": icon,
        }.items()
        if value is not UNSET
    }
    merged = {**current, **given}
    fields = _clean(
        session,
        merged,
        check_account=merged["linked_account_id"] != current["linked_account_id"],
    )
    if fields == current:
        raise ValueError("Nothing has changed.")
    change = Change("goal", str(goal_id), before, {**before, **fields})
    preview = make_preview(
        session,
        "goal.update",
        [change],
        summary={"label": f"Updated {fields['name']}", "goal_id": goal_id},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def archive_goal(session: Session, goal_id: int, archived: bool, *, idempotency_key: str) -> Batch:
    """Archive a goal, or restore it. An archived goal leaves the flex number and the goal totals
    by default. Raises LookupError if there is no such goal, and ValueError when it already has
    that state."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = snapshot(session, "goal", goal_id)
    if before is None:
        raise LookupError(f"There is no goal {goal_id}.")
    archived = bool(archived)
    if bool(before["archived"]) == archived:
        raise ValueError("Nothing has changed.")
    change = Change("goal", str(goal_id), before, {**before, "archived": archived})
    label = f"Archived {before['name']}" if archived else f"Restored {before['name']}"
    preview = make_preview(
        session, "goal.archive", [change], summary={"label": label, "goal_id": goal_id}
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def delete_goal(session: Session, goal_id: int, *, idempotency_key: str) -> Batch:
    """Delete a goal as one batch, undoable while nothing has changed since. Raises LookupError
    if there is no such goal."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = snapshot(session, "goal", goal_id)
    if before is None:
        raise LookupError(f"There is no goal {goal_id}.")
    change = Change("goal", str(goal_id), before, None)
    preview = make_preview(
        session,
        "goal.delete",
        [change],
        summary={"label": f"Deleted {before['name']}", "goal_id": goal_id},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


# Reads


def goal_progress(session: Session, goal: Goal, *, today: date) -> GoalProgress:
    """A goal's progress on `today`. Progress follows the linked account's balance: saved is that
    account's latest ledger statement balance, clamped at 0. A closed account still counts, and
    available balances are ignored. Without a linked account, or before the account has a ledger
    balance, saved is 0."""
    saved, as_of = _saved(session, goal.linked_account_id)
    target = goal.target_cents
    remaining = max(target - saved, 0)
    if target <= 0:
        percent = BASIS_POINTS
    else:
        percent = min(saved * BASIS_POINTS // target, BASIS_POINTS)
    projected = _projected_date(today, remaining, goal.monthly_contribution_cents)
    return GoalProgress(
        goal_id=goal.id,
        target_cents=target,
        saved_cents=saved,
        remaining_cents=remaining,
        percent_bp=percent,
        monthly_contribution_cents=goal.monthly_contribution_cents,
        projected_date=projected,
        required_monthly_cents=_required_monthly(today, remaining, goal.target_date),
        status=_status(remaining, goal.target_date, projected),
        balance_as_of=as_of,
    )


def goals_summary(session: Session, *, today: date, include_archived: bool = False) -> GoalsSummary:
    """The goals with their progress, active ones first, then totals and a count per status.
    Archived goals are left out unless include_archived is True."""
    query = select(Goal).order_by(Goal.archived, Goal.id)
    if not include_archived:
        query = query.where(Goal.archived.is_(False))
    goals = session.scalars(query).all()
    rows = tuple((goal, goal_progress(session, goal, today=today)) for goal in goals)
    counts = dict.fromkeys(GoalStatus, 0)
    for _goal, progress in rows:
        counts[progress.status] += 1
    return GoalsSummary(
        rows=rows,
        target_cents=sum(goal.target_cents for goal in goals),
        saved_cents=sum(progress.saved_cents for _goal, progress in rows),
        monthly_contribution_cents=sum(goal.monthly_contribution_cents for goal in goals),
        status_counts=counts,
    )


# Helpers


def _clean(session: Session, fields: dict[str, Any], *, check_account: bool) -> dict[str, Any]:
    """Check every field against its rule and return the cleaned values. The linked account is
    looked up only when check_account is True and an account is given."""
    name = fields["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Give the goal a name.")
    target = _whole_cents(fields["target_cents"], "The target")
    if target <= 0:
        raise ValueError("The target must be more than zero.")
    contribution = _whole_cents(fields["monthly_contribution_cents"], "The monthly contribution")
    if contribution < 0:
        raise ValueError("The monthly contribution cannot be negative.")
    target_date = fields["target_date"]
    if target_date is not None and not isinstance(target_date, date):
        raise TypeError("The target date must be a date.")
    account_id = fields["linked_account_id"]
    if account_id is not None and check_account:
        account = session.get(Account, account_id)
        if account is None:
            raise ValueError(f"There is no account {account_id}.")
        if account.closed:
            raise ValueError(f"{account.name} is closed, so a goal cannot be linked to it.")
    color = _optional_text(fields["color"])
    if color is not None and not COLOR_PATTERN.fullmatch(color):
        raise ValueError("The colour must look like #RRGGBB.")
    icon = _optional_text(fields["icon"])
    if icon is not None and (len(icon) > ICON_MAX or not ICON_PATTERN.fullmatch(icon)):
        raise ValueError("The icon must be a short slug, such as piggy-bank.")
    return {
        "name": name.strip(),
        "target_cents": target,
        "target_date": target_date,
        "linked_account_id": account_id,
        "monthly_contribution_cents": contribution,
        "color": color,
        "icon": icon,
    }


def _editable(goal: Goal) -> dict[str, Any]:
    return {
        "name": goal.name,
        "target_cents": goal.target_cents,
        "target_date": goal.target_date,
        "linked_account_id": goal.linked_account_id,
        "monthly_contribution_cents": goal.monthly_contribution_cents,
        "color": goal.color,
        "icon": goal.icon,
    }


def _whole_cents(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be a whole number of cents")
    return value


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _saved(session: Session, account_id: int | None) -> tuple[int, date | None]:
    """The linked account's latest ledger balance, clamped at 0, and its date. On a date with two
    balances, the one imported last is used, as the accounts panel does."""
    if account_id is None:
        return 0, None
    row = session.execute(
        select(StatementBalance.as_of, StatementBalance.balance_cents)
        .where(
            StatementBalance.account_id == account_id,
            StatementBalance.kind == BalanceKind.ledger,
        )
        .order_by(StatementBalance.as_of.desc(), StatementBalance.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return 0, None
    return max(row.balance_cents, 0), row.as_of


def _projected_date(today: date, remaining: int, contribution: int) -> date | None:
    if remaining == 0:
        return today
    if contribution == 0:
        return None
    try:
        return shift_months(today, _ceil_div(remaining, contribution))
    except (ValueError, OverflowError):  # past year 9999: no date to show, never reached
        return None


def _required_monthly(today: date, remaining: int, target_date: date | None) -> int | None:
    """The contribution that spreads what remains over the whole months left. Those are the
    months, from today, that end on or before the target date, and there is at least one."""
    if target_date is None:
        return None
    months = max(_whole_months_until(today, target_date), 1)
    return _ceil_div(remaining, months)


def _status(remaining: int, target_date: date | None, projected: date | None) -> GoalStatus:
    if remaining == 0:
        return GoalStatus.complete
    if target_date is None:
        return GoalStatus.on_track
    if projected is not None and projected <= target_date:
        return GoalStatus.on_track
    return GoalStatus.behind


def _whole_months_until(today: date, target: date) -> int:
    """The largest n >= 0 such that today plus n months is on or before target, or 0 if none is."""
    n = max((target.year - today.year) * 12 + target.month - today.month, 0)
    while n > 0 and shift_months(today, n) > target:
        n -= 1
    while shift_months(today, n + 1) <= target:
        n += 1
    return n


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)
