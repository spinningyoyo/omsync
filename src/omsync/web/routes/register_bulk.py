"""Register bulk-edit forms, previews, and application responses."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import CHANGED_ELSEWHERE, BatchError, StalePreview, seen_is_current
from omsync.ledger.bulk import BulkPlan, apply_bulk, ids_matching, plan_bulk
from omsync.ledger.filters import FilterError, FilterSpec
from omsync.ledger.register import categories
from omsync.ledger.tags import list_tags
from omsync.web.deps import get_db
from omsync.web.register_replies import RegisterError, fragment, reply
from omsync.web.register_state import (
    RegisterState,
    build_state,
    category_id_from,
    decode_filter,
    page_rows,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
VALUE_ACTIONS = ("set_category", "set_memo", "add_tag", "remove_tag")
ACTIONS = (*VALUE_ACTIONS, "clear_category")


@router.post("/transactions/bulk/form", response_class=HTMLResponse)
def bulk_form(
    request: Request,
    db: DbSession,
    action: Annotated[str, Form(alias="bulk_action")],
) -> HTMLResponse:
    """The first step of "Set category…", "Set memo…", or "Tag…": the value to set."""
    if action not in VALUE_ACTIONS:
        raise RegisterError("Choose what to change.", 400)
    return fragment(
        request,
        "frag_bulk_value",
        action=action,
        categories=categories(db),
        tags=list_tags(db),
    )


@router.post("/transactions/bulk/preview", response_class=HTMLResponse)
def bulk_preview(
    request: Request,
    db: DbSession,
    action: Annotated[str, Form(alias="bulk_action")],
    value: Annotated[str, Form()] = "",
    ids: Annotated[list[int] | None, Form()] = None,
    select_all: Annotated[str, Form()] = "0",
    f: Annotated[str | None, Form()] = None,
    account: Annotated[int | None, Form()] = None,
    saved: Annotated[int | None, Form()] = None,
) -> HTMLResponse:
    """Count what the change would do. Writes nothing. Apply sends back the revision counted at."""
    try:
        _, _txn_ids, plan = _plan(db, action, value, ids, select_all, f, account, saved)
    except (ValueError, LookupError, FilterError) as exc:
        return fragment(request, "frag_bulk_error", status_code=422, message=str(exc))
    return fragment(
        request,
        "frag_bulk_preview",
        plan=plan,
        action=action,
        value=value,
        revision=plan.preview.base_revision,
        key=uuid4().hex,
    )


@router.post("/transactions/bulk/apply", response_class=HTMLResponse)
def bulk_apply(
    request: Request,
    db: DbSession,
    action: Annotated[str, Form(alias="bulk_action")],
    revision: Annotated[int, Form()],
    value: Annotated[str, Form()] = "",
    ids: Annotated[list[int] | None, Form()] = None,
    select_all: Annotated[str, Form()] = "0",
    f: Annotated[str | None, Form()] = None,
    account: Annotated[int | None, Form()] = None,
    saved: Annotated[int | None, Form()] = None,
    page: Annotated[int, Form()] = 1,
    sort: Annotated[str | None, Form()] = None,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Apply the change as one batch, if the data is still at the revision the counts came from.
    The register stays on its page."""
    try:
        state, _, plan = _plan(db, action, value, ids, select_all, f, account, saved)
    except (ValueError, LookupError, FilterError) as exc:
        return fragment(request, "frag_bulk_error", status_code=422, message=str(exc))
    key = idempotency_key or uuid4().hex
    if not seen_is_current(db, plan.preview, revision, key):
        return fragment(request, "frag_bulk_error", status_code=409, message=CHANGED_ELSEWHERE)
    try:
        batch = apply_bulk(db, plan, idempotency_key=key)
    except StalePreview:
        return fragment(request, "frag_bulk_error", status_code=409, message=CHANGED_ELSEWHERE)
    except BatchError as exc:
        return fragment(request, "frag_bulk_error", status_code=422, message=str(exc))
    state = build_state(
        db,
        account_id=state.account_id,
        spec=state.spec,
        saved_id=None if state.saved is None else state.saved.id,
        page=page,
        sort=sort,
    )  # the counts and the rows as they are after the change
    return reply(
        request,
        state,
        page_rows(db, state),
        block="frag_bulk_applied",
        events=("register-reset", "bulk-done"),
        url_mode="none",
        label=plan.label,
        batch_id=batch.id,
    )


def _plan(
    db: Session,
    action: str,
    value: str,
    ids: list[int] | None,
    select_all: str,
    f: str | None,
    account: int | None,
    saved: int | None,
) -> tuple[RegisterState, list[int], BulkPlan]:
    """The plan for this selection and change. Raises ValueError, LookupError, or FilterError with
    a message for the dialog."""
    if action not in ACTIONS:
        raise ValueError("Choose what to change.")
    spec: FilterSpec = decode_filter(f)
    state = build_state(db, account_id=account, spec=spec, saved_id=saved)
    txn_ids = ids_matching(db, spec, account_id=account) if select_all == "1" else list(ids or [])
    if action == "set_category":
        category_id = category_id_from(value)
        if category_id is None:
            raise ValueError("Pick a category from the list.")
        plan = plan_bulk(db, txn_ids, set_category_id=category_id)
    elif action == "clear_category":
        plan = plan_bulk(db, txn_ids, clear_category=True)
    elif action == "add_tag":
        try:
            tag_id = int(value)
        except (ValueError, TypeError):
            raise ValueError("Pick a tag from the list.") from None
        plan = plan_bulk(db, txn_ids, add_tag_id=tag_id)
    elif action == "remove_tag":
        try:
            tag_id = int(value)
        except (ValueError, TypeError):
            raise ValueError("Pick a tag from the list.") from None
        plan = plan_bulk(db, txn_ids, remove_tag_id=tag_id)
    else:
        plan = plan_bulk(db, txn_ids, set_memo=value)
    return state, txn_ids, plan
