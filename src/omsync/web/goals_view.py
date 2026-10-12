"""View data for the goal pages and the goals card: the list, a goal's detail page, and the
create and edit forms. The numbers come from omsync.plan.goals. This module formats them and works
out what the templates need: bar widths, status tones, projection and callout text, and the forms'
idempotency keys. It makes no writes.

Progress comes from the domain's basis points. Money is formatted with format_cents, and no value
here is a float.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.ledger.models import Account, Goal
from omsync.ledger.sources import account_digits
from omsync.plan.goals import BASIS_POINTS, GoalProgress, GoalStatus, goal_progress, goals_summary
from omsync.web.formats import fmt_date, money_input

DEFAULT_COLOR = "#2E90FA"
GOAL_COLORS: tuple[str, ...] = (
    "#2E90FA",  # blue-500
    "#12B76A",  # green-500
    "#F79009",  # yellow-500
    "#F13636",  # red-500
    "#F23E94",  # pink-500
    "#875BF7",  # violet-500
    "#06AED4",  # cyan-500
    "#6172F3",  # indigo-500
)
GOAL_ICONS: tuple[tuple[str, str], ...] = (
    ("piggy-bank", "Savings"),
    ("shield", "Emergency fund"),
    ("plane", "Travel"),
    ("car", "Car"),
    ("house", "Home"),
    ("graduation-cap", "Education"),
    ("gift", "Gift"),
    ("heart", "Health"),
    ("wallet", "Wallet"),
    ("target", "Other"),
)
DEFAULT_ICON = "piggy-bank"
COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
STATUS_LABEL = {
    GoalStatus.complete: "Complete",
    GoalStatus.on_track: "On track",
    GoalStatus.behind: "Behind",
}
STATUS_TONE = {
    GoalStatus.complete: "info",
    GoalStatus.on_track: "success",
    GoalStatus.behind: "warning",
}


@dataclass(frozen=True)
class GoalRow:
    goal_id: int
    name: str
    icon: str
    color: str
    saved: str
    target: str
    width: int
    bar: str
    status: str
    status_label: str
    status_tone: str
    projection: str
    archived: bool
    href: str


@dataclass(frozen=True)
class GoalsPage:
    rows: tuple[GoalRow, ...]
    include_archived: bool
    saved_total: str
    target_total: str
    contribution_total: str
    counts: tuple[tuple[str, int, str], ...]  # label, count, tone
    archived_count: int


@dataclass(frozen=True)
class GoalsCard:
    rows: tuple[GoalRow, ...]
    count: int
    saved_total: str
    target_total: str
    counts: tuple[tuple[str, int, str], ...]


@dataclass(frozen=True)
class GoalForm:
    """A goal form for the create or edit dialog. Values are strings, as typed, so a refused form
    shows what was typed. `errors` maps a field to its message; `error` is for the whole form."""

    body_id: str
    title: str
    action: str
    submit_label: str
    key: str
    name: str = ""
    target: str = ""
    target_date: str = ""
    account_id: str = ""
    contribution: str = ""
    color: str = DEFAULT_COLOR
    icon: str = DEFAULT_ICON
    errors: dict[str, str] = field(default_factory=dict)
    error: str = ""
    accounts: tuple[tuple[str, str], ...] = ()
    colors: tuple[str, ...] = GOAL_COLORS
    icons: tuple[tuple[str, str], ...] = GOAL_ICONS


@dataclass(frozen=True)
class GoalPage:
    id: int
    name: str
    icon: str
    color: str
    archived: bool
    saved: str
    target: str
    remaining: str
    width: int
    bar: str
    status: str
    status_label: str
    status_tone: str
    callout_title: str
    callout_text: str
    projection: str
    required: str
    balance: str
    account: str
    account_href: str
    target_date: str
    contribution: str
    archive_key: str
    delete_key: str
    edit: GoalForm


def goal_row(goal: Goal, progress: GoalProgress, *, today: date) -> GoalRow:
    """One goal as a row in a list or a card."""
    if progress.status is GoalStatus.complete:
        projection = "Complete"
    elif progress.projected_date is None:
        projection = "Too far to project" if goal.monthly_contribution_cents else "No contribution"
    else:
        projection = f"Reached {fmt_date(progress.projected_date)}"
    width, bar = _goal_bar(progress)
    return GoalRow(
        goal_id=goal.id,
        name=goal.name,
        icon=_icon(goal.icon),
        color=_color(goal.color),
        saved=format_cents(progress.saved_cents),
        target=format_cents(progress.target_cents),
        width=width,
        bar=bar,
        status=progress.status.value,
        status_label=STATUS_LABEL[progress.status],
        status_tone=STATUS_TONE[progress.status],
        projection=projection,
        archived=bool(goal.archived),
        href=f"/plan/goals/{goal.id}",
    )


def goals_page(db: Session, *, today: date, include_archived: bool) -> GoalsPage:
    summary = goals_summary(db, today=today, include_archived=include_archived)
    archived = goals_summary(db, today=today, include_archived=True)
    archived_count = sum(1 for goal, _p in archived.rows if goal.archived)
    return GoalsPage(
        rows=tuple(goal_row(goal, progress, today=today) for goal, progress in summary.rows),
        include_archived=include_archived,
        saved_total=format_cents(summary.saved_cents),
        target_total=format_cents(summary.target_cents),
        contribution_total=format_cents(summary.monthly_contribution_cents),
        counts=_counts(summary.status_counts),
        archived_count=archived_count,
    )


def goals_card(db: Session, *, today: date) -> GoalsCard:
    """The goals card: the first three active goals, with the totals and status counts of all of
    them. It needs only the session and today, so any page can show it."""
    summary = goals_summary(db, today=today)
    return GoalsCard(
        rows=tuple(goal_row(goal, progress, today=today) for goal, progress in summary.rows[:3]),
        count=len(summary.rows),
        saved_total=format_cents(summary.saved_cents),
        target_total=format_cents(summary.target_cents),
        counts=_counts(summary.status_counts),
    )


def goal_page(db: Session, goal_id: int, *, today: date) -> GoalPage:
    """A goal's detail page. Raises LookupError if there is no such goal."""
    goal = db.get(Goal, goal_id)
    if goal is None:
        raise LookupError(f"There is no goal {goal_id}.")
    progress = goal_progress(db, goal, today=today)
    account = db.get(Account, goal.linked_account_id) if goal.linked_account_id else None
    width, bar = _goal_bar(progress)
    callout_title, callout_text = _callout(goal, progress)
    return GoalPage(
        id=goal.id,
        name=goal.name,
        icon=_icon(goal.icon),
        color=_color(goal.color),
        archived=bool(goal.archived),
        saved=format_cents(progress.saved_cents),
        target=format_cents(progress.target_cents),
        remaining=format_cents(progress.remaining_cents),
        width=width,
        bar=bar,
        status=progress.status.value,
        status_label=STATUS_LABEL[progress.status],
        status_tone=STATUS_TONE[progress.status],
        callout_title=callout_title,
        callout_text=callout_text,
        projection=_projection_text(goal, progress),
        required=_required_text(goal, progress),
        balance=_balance_text(account, progress),
        account=_account_text(account),
        account_href=f"/accounts/{account.id}" if account is not None else "",
        target_date=fmt_date(goal.target_date) if goal.target_date else "No target date",
        contribution=(
            f"{format_cents(goal.monthly_contribution_cents)} a month"
            if goal.monthly_contribution_cents
            else "No monthly contribution"
        ),
        archive_key=uuid4().hex,
        delete_key=uuid4().hex,
        edit=goal_form(
            db,
            body_id="goal-edit-body",
            title="Edit goal",
            action=f"/plan/goals/{goal.id}",
            submit_label="Save goal",
            goal=goal,
        ),
    )


def goal_form(
    db: Session,
    *,
    body_id: str,
    title: str,
    action: str,
    submit_label: str,
    goal: Goal | None = None,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    error: str = "",
) -> GoalForm:
    """A goal form. Values come from the goal when one is given, then from `values`, which hold what
    was typed in a refused form. An account that is closed still shows when a goal is linked to it,
    so saving does not silently unlink it."""
    base = _form_values(goal)
    base.update(values or {})
    accounts = _account_choices(db, goal)
    colors = GOAL_COLORS
    if base["color"] not in colors and COLOR_RE.fullmatch(base["color"]):
        colors = (*colors, base["color"])
    icons = GOAL_ICONS
    if base["icon"] not in {name for name, _label in GOAL_ICONS}:
        base["icon"] = DEFAULT_ICON
    return GoalForm(
        body_id=body_id,
        title=title,
        action=action,
        submit_label=submit_label,
        key=uuid4().hex,
        name=base["name"],
        target=base["target"],
        target_date=base["target_date"],
        account_id=base["account_id"],
        contribution=base["contribution"],
        color=base["color"],
        icon=base["icon"],
        errors=dict(errors or {}),
        error=error,
        accounts=accounts,
        colors=colors,
        icons=icons,
    )


def new_goal_form(db: Session) -> GoalForm:
    """The empty form in the New goal dialog."""
    return goal_form(
        db,
        body_id="goal-create-body",
        title="New goal",
        action="/plan/goals",
        submit_label="Add goal",
    )


def _form_values(goal: Goal | None) -> dict[str, str]:
    if goal is None:
        return {
            "name": "",
            "target": "",
            "target_date": "",
            "account_id": "",
            "contribution": "",
            "color": DEFAULT_COLOR,
            "icon": DEFAULT_ICON,
        }
    return {
        "name": goal.name,
        "target": money_input(goal.target_cents),
        "target_date": goal.target_date.isoformat() if goal.target_date else "",
        "account_id": str(goal.linked_account_id) if goal.linked_account_id else "",
        "contribution": money_input(goal.monthly_contribution_cents),
        "color": _color(goal.color),
        "icon": _icon(goal.icon),
    }


def _account_choices(db: Session, goal: Goal | None) -> tuple[tuple[str, str], ...]:
    accounts = list(
        db.scalars(select(Account).where(Account.closed.is_(False)).order_by(Account.name))
    )
    if goal is not None and goal.linked_account_id is not None:
        linked = db.get(Account, goal.linked_account_id)
        if linked is not None and linked.closed:
            accounts.append(linked)
    return (("", "No account"), *((str(a.id), _account_text(a)) for a in accounts))


def _goal_bar(progress: GoalProgress) -> tuple[int, str]:
    width = min(progress.percent_bp, BASIS_POINTS) // 100
    bar = "bg-warning" if progress.status is GoalStatus.behind else "bg-inverse"
    return width, bar


def _callout(goal: Goal, progress: GoalProgress) -> tuple[str, str]:
    if progress.status is GoalStatus.complete:
        return "Complete", "You have reached the target."
    if goal.target_date is None:
        if progress.projected_date is None and not goal.monthly_contribution_cents:
            return "On track", "No target date is set. Add a monthly contribution to plan it."
        return "On track", "No target date is set."
    target = fmt_date(goal.target_date)
    if progress.status is GoalStatus.on_track:
        return (
            "On track",
            f"Projected {fmt_date(progress.projected_date)}, on or before {target}.",
        )
    if progress.projected_date is None:
        if goal.monthly_contribution_cents:
            return "Behind", f"Too far away to project, so it will not be reached by {target}."
        return "Behind", f"Nothing is contributed, so the target will not be reached by {target}."
    return "Behind", f"Projected {fmt_date(progress.projected_date)}, after {target}."


def _projection_text(goal: Goal, progress: GoalProgress) -> str:
    if progress.status is GoalStatus.complete:
        return "Complete. Nothing is left to save."
    if progress.projected_date is None:
        if goal.monthly_contribution_cents:
            return (
                f"At {format_cents(goal.monthly_contribution_cents)} a month, the target is too "
                "far away to project."
            )
        return "Add a monthly contribution to see when this goal is reached."
    return (
        f"At {format_cents(goal.monthly_contribution_cents)} a month, the target is reached on "
        f"{fmt_date(progress.projected_date)}."
    )


def _required_text(goal: Goal, progress: GoalProgress) -> str:
    if goal.target_date is None or progress.remaining_cents == 0:
        return ""
    if progress.required_monthly_cents is None:
        return ""
    return (
        f"To reach it by {fmt_date(goal.target_date)}, save "
        f"{format_cents(progress.required_monthly_cents)} a month."
    )


def _balance_text(account: Account | None, progress: GoalProgress) -> str:
    if progress.balance_as_of is not None and account is not None:
        return f"Saved is {account.name}'s balance on {fmt_date(progress.balance_as_of)}."
    if account is not None:
        return f"{account.name} has no ledger balance yet, so saved is $0.00."
    return "No account is linked, so saved is $0.00."


def _account_text(account: Account | None) -> str:
    if account is None:
        return ""
    if account.last4:
        return f"{account.name} ({account_digits(account.last4, account.alias_last4)})"
    return account.name


def _icon(icon: str | None) -> str:
    known = {name for name, _label in GOAL_ICONS}
    return icon if icon in known else "target"


def _color(color: str | None) -> str:
    return color if color and COLOR_RE.fullmatch(color) else DEFAULT_COLOR


def _counts(status_counts: dict[GoalStatus, int]) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (STATUS_LABEL[status], status_counts.get(status, 0), STATUS_TONE[status])
        for status in GoalStatus
    )
