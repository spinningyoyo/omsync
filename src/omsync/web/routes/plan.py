"""Plan hub, monthly budget pages, and budget edit fragments."""

import dataclasses
from datetime import date
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.core.money import format_cents
from omsync.core.months import shift_month_start
from omsync.ledger.models import Category
from omsync.plan import budgets
from omsync.web import budget_view, goals_view
from omsync.web.deps import get_db
from omsync.web.formats import month_key, month_title, parse_month
from omsync.web.plan_replies import fragment, money_field, refused
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
HUB = "pages/plan_hub.html"
BUDGET = "pages/plan_budget.html"
MODE_LABELS = {
    "flex": "Switched to flex budgeting",
    "category": "Switched to category budgeting",
}


# Pages


@router.get("/plan", response_class=HTMLResponse)
def plan_hub(request: Request, db: DbSession) -> HTMLResponse:
    today = date.today()
    context = shell_context(request, db, active="plan", title="Plan")
    return request.app.state.templates.TemplateResponse(
        request,
        HUB,
        {
            **context,
            "budget": budget_view.budget_card(db, today=today),
            "goals": goals_view.goals_card(db, today=today),
        },
    )


@router.get("/plan/budget", response_class=HTMLResponse)
def budget_this_month() -> RedirectResponse:
    return RedirectResponse(f"/plan/budget/{month_key(date.today())}", status_code=303)


@router.get("/plan/budget/{month}", response_class=HTMLResponse)
def budget_page(request: Request, db: DbSession, month: str) -> HTMLResponse:
    viewed = _month_or_404(month)
    context = shell_context(request, db, active="plan", title="Plan")
    page = budget_view.budget_page(db, viewed, today=date.today())
    return request.app.state.templates.TemplateResponse(request, BUDGET, {**context, "page": page})


# Budget writes


@router.post("/plan/budget/{month}/mode", response_class=HTMLResponse)
def set_mode(
    request: Request,
    db: DbSession,
    month: str,
    mode: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    viewed = _month_or_404(month)
    if mode not in MODE_LABELS:
        return _budget_reply(
            request, db, viewed, error="Choose flex or category budgeting.", status=422
        )
    try:
        batch = budgets.set_mode(db, mode, idempotency_key=_key(idempotency_key))
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _budget_reply(request, db, viewed, error=error, status=status)
    return _budget_reply(request, db, viewed, message=MODE_LABELS[mode], batch_id=batch.id)


@router.post("/plan/budget/{month}/income", response_class=HTMLResponse)
def set_income(
    request: Request,
    db: DbSession,
    month: str,
    amount: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    viewed = _month_or_404(month)
    try:
        cents = money_field(amount, "the expected income")
        batch = budgets.set_expected_income(db, cents, idempotency_key=_key(idempotency_key))
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _budget_reply(request, db, viewed, error=error, status=status)
    message = f"Expected income set to {format_cents(cents)}"
    return _budget_reply(request, db, viewed, message=message, batch_id=batch.id)


@router.post("/plan/budget/{month}/income/average", response_class=HTMLResponse)
def use_average_income(
    request: Request,
    db: DbSession,
    month: str,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    viewed = _month_or_404(month)
    try:
        batch = budgets.set_expected_income(db, None, idempotency_key=_key(idempotency_key))
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _budget_reply(request, db, viewed, error=error, status=status)
    return _budget_reply(
        request,
        db,
        viewed,
        message="Expected income now uses the three-month average",
        batch_id=batch.id,
    )


@router.post("/plan/budget/{month}/lines", response_class=HTMLResponse)
def save_line(
    request: Request,
    db: DbSession,
    month: str,
    category_id: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Set one category's amount for the month. Its rollover setting stays as it is."""
    viewed = _month_or_404(month)
    cid = _category_id(category_id)
    try:
        cents = money_field(amount, "a budget amount")
        batch = budgets.set_line(
            db, viewed, cid, "category", cents, idempotency_key=_key(idempotency_key)
        )
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _row_reply(request, db, viewed, cid, status=status, error=error, typed=amount)
    message = f"Budget for {_category_name(db, cid)} set to {format_cents(cents)}"
    return _row_reply(request, db, viewed, cid, message=message, batch_id=batch.id)


@router.post("/plan/budget/{month}/lines/rollover", response_class=HTMLResponse)
def set_rollover(
    request: Request,
    db: DbSession,
    month: str,
    category_id: Annotated[str, Form()] = "",
    rollover: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Turn a category's rollover on or off. Its amount for the month stays as it is."""
    viewed = _month_or_404(month)
    cid = _category_id(category_id)
    on = rollover == "1"
    try:
        amount = budget_view.line_amount(db, viewed, cid)
        batch = budgets.set_line(
            db, viewed, cid, "category", amount, rollover=on, idempotency_key=_key(idempotency_key)
        )
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _row_reply(request, db, viewed, cid, status=status, error=error)
    state = "on" if on else "off"
    message = f"Rollover {state} for {_category_name(db, cid)}"
    return _row_reply(request, db, viewed, cid, message=message, batch_id=batch.id)


@router.post("/plan/budget/{month}/lines/new", response_class=HTMLResponse)
def add_budget(
    request: Request,
    db: DbSession,
    month: str,
    category_id: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Give an unbudgeted category its first line. The category moves into its table, so the whole
    budget block is re-rendered rather than one row."""
    viewed = _month_or_404(month)
    cid = _category_id(category_id)
    try:
        cents = money_field(amount, "a budget amount")
        batch = budgets.set_line(
            db, viewed, cid, "category", cents, idempotency_key=_key(idempotency_key)
        )
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _budget_reply(request, db, viewed, error=error, status=status)
    message = f"Budget for {_category_name(db, cid)} set to {format_cents(cents)}"
    return _budget_reply(request, db, viewed, message=message, batch_id=batch.id)


@router.post("/plan/budget/{month}/copy", response_class=HTMLResponse)
def copy_last_month(
    request: Request,
    db: DbSession,
    month: str,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    viewed = _month_or_404(month)
    source = shift_month_start(viewed, -1)
    try:
        batch = budgets.copy_month(db, source, viewed, idempotency_key=_key(idempotency_key))
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _budget_reply(request, db, viewed, error=error, status=status)
    message = f"Copied the budget lines from {month_title(source)}"
    return _budget_reply(request, db, viewed, message=message, batch_id=batch.id)


@router.post("/plan/budget/{month}/fill-fixed", response_class=HTMLResponse)
def fill_fixed_from_last_month(
    request: Request,
    db: DbSession,
    month: str,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Give the fixed categories that have no line this month last month's amounts."""
    viewed = _month_or_404(month)
    source = shift_month_start(viewed, -1)
    try:
        batch = budgets.fill_fixed_from_previous_month(
            db, viewed, idempotency_key=_key(idempotency_key)
        )
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _budget_reply(request, db, viewed, error=error, status=status)
    filled = len(batch.summary["category_ids"])
    noun = "budget" if filled == 1 else "budgets"
    message = f"Filled {filled} fixed {noun} from {month_title(source)}."
    return _budget_reply(request, db, viewed, message=message, batch_id=batch.id)


@router.post("/plan/budget/{month}/flex-number", response_class=HTMLResponse)
def set_flex_number(
    request: Request,
    db: DbSession,
    month: str,
    amount: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Set the flex number by hand for one month, in place of the one worked out from the budget."""
    viewed = _month_or_404(month)
    try:
        cents = money_field(amount, "the flex number")
        batch = budgets.set_line(
            db, viewed, None, "flex_pool", cents, idempotency_key=_key(idempotency_key)
        )
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _budget_reply(request, db, viewed, error=error, status=status)
    message = f"Flex number set to {format_cents(cents)} by hand"
    return _budget_reply(request, db, viewed, message=message, batch_id=batch.id)


@router.post("/plan/budget/{month}/flex-number/reset", response_class=HTMLResponse)
def reset_flex_number(
    request: Request,
    db: DbSession,
    month: str,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    viewed = _month_or_404(month)
    try:
        batch = budgets.clear_flex_pool(db, viewed, idempotency_key=_key(idempotency_key))
    except (ValueError, BatchError) as exc:
        status, error = refused(exc)
        return _budget_reply(request, db, viewed, error=error, status=status)
    message = "The flex number is worked out from your budget again"
    return _budget_reply(request, db, viewed, message=message, batch_id=batch.id)


# Undo


def plan_undo_reply(request: Request, db: Session, plan: str | None) -> HTMLResponse:
    """The reply to undoing a plan batch. `plan` names the page the toast came from: budget:YYYY-MM,
    goals or goals:archived, or goal:ID. That page's regions are re-rendered out of band, so the
    change the undo reverses shows at once. A goal that no longer exists gets the toast alone."""
    today = date.today()
    kind, _, arg = (plan or "").partition(":")
    page = goals = goal = None
    if kind == "budget":
        viewed = parse_month(arg)
        if viewed is not None:
            page = budget_view.budget_page(db, viewed, today=today)
    elif kind == "goals":
        goals = goals_view.goals_page(db, today=today, include_archived=arg == "archived")
    elif kind == "goal" and arg.isdigit():
        try:
            goal = goals_view.goal_page(db, int(arg), today=today)
        except LookupError:
            goal = None
    return fragment(
        request,
        "frag_undone",
        message="Undone",
        page=page,
        goals=goals,
        goal=goal,
        create=goals_view.new_goal_form(db),
    )


# Replies


def _budget_reply(
    request: Request,
    db: Session,
    viewed: date,
    *,
    error: str = "",
    status: int = 200,
    message: str = "",
    batch_id: int | None = None,
) -> HTMLResponse:
    """The budget page's main block, out of band, with its toast and any refusal."""
    page = budget_view.budget_page(db, viewed, today=date.today())
    return fragment(
        request,
        "frag_budget_main",
        status_code=status,
        page=page,
        error=error,
        message=message,
        batch_id=batch_id,
        plan=_plan_token(viewed),
    )


def _row_reply(
    request: Request,
    db: Session,
    viewed: date,
    category_id: int,
    *,
    status: int = 200,
    error: str = "",
    typed: str | None = None,
    message: str = "",
    batch_id: int | None = None,
) -> HTMLResponse:
    """One category's row, first, so htmx swaps it in place. A success also carries the summary and
    the toast out of band. A refusal answers with the row alone, showing the typed amount and the
    message. A row the page no longer shows falls back to the whole block."""
    page = budget_view.budget_page(db, viewed, today=date.today())
    row = page.find_row(category_id)
    if row is None:
        return _budget_reply(
            request, db, viewed, error=error, status=status, message=message, batch_id=batch_id
        )
    if error:
        row = dataclasses.replace(
            row, budget_input=row.budget_input if typed is None else typed, error=error
        )
        return fragment(
            request, "frag_line_row", status_code=status, row=row, page=page, summary=False
        )
    return fragment(
        request,
        "frag_line_row",
        row=row,
        page=page,
        summary=True,
        message=message,
        batch_id=batch_id,
        plan=_plan_token(viewed),
    )


# Helpers


def _month_or_404(text: str) -> date:
    viewed = parse_month(text)
    if viewed is None:
        raise HTTPException(status_code=404, detail="No such month")
    return viewed


def _plan_token(viewed: date) -> str:
    return f"budget:{month_key(viewed)}"


def _category_id(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise HTTPException(status_code=400, detail="No such category") from None


def _category_name(db: Session, category_id: int) -> str:
    category = db.get(Category, category_id)
    return category.name if category is not None else "that category"


def _key(value: str) -> str:
    """The key the form sent, or a fresh one. A form always sends its own, so this only covers a
    request made without the page."""
    return value or uuid4().hex
