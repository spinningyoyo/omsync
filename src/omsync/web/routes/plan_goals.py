"""The goal pages: /plan/goals, with its archived toggle, and /plan/goals/{id}, where a goal
is edited, archived, restored, or deleted.

Each write is one batch from omsync.plan.goals, with a key the page made when it rendered. A form
that is refused answers 422 with its message inside the dialog it came from, and a change answers
with the goal's region out of band and a toast. The create and edit dialogs close on the
close-dialogs event. Undo for these batches is plan.plan_undo_reply, reached from transactions.py.
"""

from datetime import date
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError
from omsync.core.months import parse_day
from omsync.plan import goals
from omsync.plan.models import Goal
from omsync.web import goals_view
from omsync.web.deps import get_db
from omsync.web.plan_replies import CLOSE_DIALOGS, fragment, money_field, refused
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
LIST = "pages/plan_goals.html"
DETAIL = "pages/plan_goal.html"
CREATE_ID = "goal-create-body"
EDIT_ID = "goal-edit-body"


# Pages


@router.get("/plan/goals", response_class=HTMLResponse)
def goals_list(request: Request, db: DbSession, archived: str = "") -> HTMLResponse:
    context = shell_context(request, db, active="plan", title="Plan")
    page = goals_view.goals_page(db, today=date.today(), include_archived=archived == "1")
    return request.app.state.templates.TemplateResponse(
        request,
        LIST,
        {**context, "page": page, "create": goals_view.new_goal_form(db)},
    )


@router.get("/plan/goals/{goal_id}", response_class=HTMLResponse)
def goal_detail(request: Request, db: DbSession, goal_id: int) -> HTMLResponse:
    goal = _goal_page_or_404(db, goal_id)
    context = shell_context(request, db, active="plan", title="Plan")
    return request.app.state.templates.TemplateResponse(request, DETAIL, {**context, "goal": goal})


# Writes


@router.post("/plan/goals", response_class=HTMLResponse)
def create_goal(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form()] = "",
    target: Annotated[str, Form()] = "",
    target_date: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    contribution: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    icon: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    values = _values(name, target, target_date, account_id, contribution, color, icon)
    fields, errors = _parse(values)
    if errors:
        return _create_refused(request, db, values, errors, status=422)
    try:
        batch = goals.create_goal(db, **fields, idempotency_key=_key(idempotency_key))
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _create_refused(request, db, values, {}, error=error, status=status)
    page = goals_view.goals_page(db, today=date.today(), include_archived=False)
    return fragment(
        request,
        "frag_goal_list",
        page=page,
        create=goals_view.new_goal_form(db),
        message=_label(batch, f"Added {fields['name']}"),
        batch_id=batch.id,
        plan="goals",
        trigger=CLOSE_DIALOGS,
    )


@router.post("/plan/goals/{goal_id}", response_class=HTMLResponse)
def update_goal(
    request: Request,
    db: DbSession,
    goal_id: int,
    name: Annotated[str, Form()] = "",
    target: Annotated[str, Form()] = "",
    target_date: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    contribution: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    icon: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    goal = _goal_or_404(db, goal_id)
    values = _values(name, target, target_date, account_id, contribution, color, icon)
    fields, errors = _parse(values)
    if errors:
        return _edit_refused(request, db, goal, values, errors, status=422)
    try:
        batch = goals.update_goal(db, goal_id, **fields, idempotency_key=_key(idempotency_key))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such goal") from None
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _edit_refused(request, db, goal, values, {}, error=error, status=status)
    return fragment(
        request,
        "frag_goal_changed",
        goal=_goal_page_or_404(db, goal_id),
        message=_label(batch, "Goal saved"),
        batch_id=batch.id,
        plan=f"goal:{goal_id}",
        trigger=CLOSE_DIALOGS,
    )


@router.post("/plan/goals/{goal_id}/archive", response_class=HTMLResponse)
def archive_goal(
    request: Request,
    db: DbSession,
    goal_id: int,
    archived: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    _goal_or_404(db, goal_id)
    try:
        batch = goals.archive_goal(
            db, goal_id, archived == "1", idempotency_key=_key(idempotency_key)
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such goal") from None
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return fragment(
            request,
            "frag_goal_error",
            status_code=status,
            goal=_goal_page_or_404(db, goal_id),
            error=error,
        )
    return fragment(
        request,
        "frag_goal_changed",
        goal=_goal_page_or_404(db, goal_id),
        message=_label(batch, "Goal changed"),
        batch_id=batch.id,
        plan=f"goal:{goal_id}",
        trigger=CLOSE_DIALOGS,
    )


@router.post("/plan/goals/{goal_id}/delete", response_class=HTMLResponse)
def delete_goal(
    request: Request,
    db: DbSession,
    goal_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    name = _goal_or_404(db, goal_id).name
    try:
        batch = goals.delete_goal(db, goal_id, idempotency_key=_key(idempotency_key))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such goal") from None
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return fragment(
            request,
            "frag_goal_error",
            status_code=status,
            goal=_goal_page_or_404(db, goal_id),
            error=error,
        )
    return fragment(
        request,
        "frag_goal_deleted",
        name=name,
        message=_label(batch, f"Deleted {name}"),
        batch_id=batch.id,
        plan=f"goal:{goal_id}",
        trigger=CLOSE_DIALOGS,
    )


# Forms


def _values(
    name: str,
    target: str,
    target_date: str,
    account_id: str,
    contribution: str,
    color: str,
    icon: str,
) -> dict[str, str]:
    """What the goal form typed, as strings, so a refused form can show it again."""
    return {
        "name": name,
        "target": target,
        "target_date": target_date,
        "account_id": account_id,
        "contribution": contribution,
        "color": color,
        "icon": icon,
    }


def _parse(values: dict[str, str]) -> tuple[dict[str, object], dict[str, str]]:
    """The domain fields for a goal form, and a message for each field that is not valid. The
    domain checks the rest, such as a closed account or a target of zero."""
    errors: dict[str, str] = {}
    name = values["name"].strip()
    if not name:
        errors["name"] = "Give the goal a name."
    target = _amount(values["target"], "the target", "target", errors)
    contribution = _amount(
        values["contribution"].strip() or "0", "the monthly contribution", "contribution", errors
    )
    target_date: date | None = None
    if values["target_date"].strip():
        try:
            target_date = parse_day(values["target_date"])
        except ValueError:
            errors["target_date"] = "Enter a target date between 1900 and 2199, as YYYY-MM-DD."
    account: int | None = None
    if values["account_id"].strip():
        if values["account_id"].strip().isdigit():
            account = int(values["account_id"].strip())
        else:
            errors["account"] = "Choose an account from the list."
    fields: dict[str, object] = {
        "name": name,
        "target_cents": target,
        "target_date": target_date,
        "linked_account_id": account,
        "monthly_contribution_cents": contribution,
        "color": values["color"] or None,
        "icon": values["icon"] or None,
    }
    return fields, errors


def _amount(text: str, what: str, field: str, errors: dict[str, str]) -> int:
    try:
        return money_field(text, what)
    except ValueError as exc:
        errors[field] = str(exc)
        return 0


def _create_refused(
    request: Request,
    db: Session,
    values: dict[str, str],
    errors: dict[str, str],
    *,
    status: int,
    error: str = "",
) -> HTMLResponse:
    form = goals_view.goal_form(
        db,
        body_id=CREATE_ID,
        title="New goal",
        action="/plan/goals",
        submit_label="Add goal",
        values=values,
        errors=errors,
        error=error,
    )
    return fragment(request, "frag_goal_create_form", status_code=status, form=form)


def _edit_refused(
    request: Request,
    db: Session,
    goal: Goal,
    values: dict[str, str],
    errors: dict[str, str],
    *,
    status: int,
    error: str = "",
) -> HTMLResponse:
    form = goals_view.goal_form(
        db,
        body_id=EDIT_ID,
        title="Edit goal",
        action=f"/plan/goals/{goal.id}",
        submit_label="Save goal",
        goal=goal,
        values=values,
        errors=errors,
        error=error,
    )
    return fragment(request, "frag_goal_edit_form", status_code=status, form=form)


# Helpers


def _goal_or_404(db: Session, goal_id: int) -> Goal:
    goal = db.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="No such goal")
    return goal


def _goal_page_or_404(db: Session, goal_id: int) -> goals_view.GoalPage:
    try:
        return goals_view.goal_page(db, goal_id, today=date.today())
    except LookupError:
        raise HTTPException(status_code=404, detail="No such goal") from None


def _label(batch: Batch, fallback: str) -> str:
    return str((batch.summary or {}).get("label", fallback))


def _key(value: str) -> str:
    return value or uuid4().hex
