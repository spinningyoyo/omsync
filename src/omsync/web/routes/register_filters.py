"""Register filter forms, saved-filter responses, and paging."""

from dataclasses import replace
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import CHANGED_ELSEWHERE, BatchError, StalePreview
from omsync.ledger.filters import (
    FIELD_OPS,
    PICKED_FIELDS,
    FilterError,
    FilterSpec,
    SavedFilterInfo,
    delete_filter,
    get_filter,
    save_filter,
    update_filter,
)
from omsync.web.deps import get_db
from omsync.web.filter_forms import FormValues, blank_values, condition_from, values_for
from omsync.web.register_replies import RegisterError, fragment, reply
from omsync.web.register_state import (
    PAGE_SIZES,
    RegisterState,
    build_state,
    decode_filter,
    page_rows,
    page_size,
    save_page_size,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
Opt = Annotated[str | None, Form()]
OptInt = Annotated[int | None, Form()]


@router.get("/transactions/filter/form", response_class=HTMLResponse)
def filter_form(
    request: Request,
    db: DbSession,
    index: int | None = None,
    field: str | None = None,
    f: str | None = None,
    account: int | None = None,
    saved: int | None = None,
) -> HTMLResponse:
    """The inline form for a new condition on `field`, or for the existing condition at `index`."""
    spec = _spec(f)
    state = _state(db, account, spec, saved)
    if index is not None:
        condition = spec.conditions[_position(index, spec)]
        if condition.field in PICKED_FIELDS:
            raise RegisterError("This filter comes from a link. Remove it instead.")
        values = values_for(condition, index)
    elif field in FIELD_OPS and field not in PICKED_FIELDS:
        values = blank_values(field)
    else:
        raise RegisterError("Choose a field to filter by.")
    return fragment(request, "frag_filter_form", values=values, state=state)


@router.get("/transactions/table", response_class=HTMLResponse)
def table(
    request: Request,
    db: DbSession,
    account: int | None = None,
    f: str | None = None,
    saved: int | None = None,
    q: str | None = None,
    page: int = 1,
    focus: str | None = None,
) -> HTMLResponse:
    """The register for a filter and a page, with the search box's text in `q` when it is sent. A
    search reply leaves the chips and the search box as they are, so the field being typed in is
    not replaced. `focus` names the pager button that takes focus back after the swap."""
    spec = _spec(f)
    if q is not None:
        try:
            spec = FilterSpec(conditions=spec.conditions, match=spec.match, search=q)
        except FilterError as exc:
            raise RegisterError(str(exc), 422) from exc
    state = _state(db, account, spec, saved, page)
    return reply(
        request,
        state,
        page_rows(db, state),
        full=q is None,
        url_mode="push" if q is None else "replace",
        focus=focus,
    )


@router.post("/transactions/page-size", response_class=HTMLResponse)
def set_page_size(
    request: Request,
    db: DbSession,
    size: Annotated[int, Form()],
    page: Annotated[int, Form()] = 1,
    f: Opt = None,
    account: OptInt = None,
    saved: OptInt = None,
) -> HTMLResponse:
    """Save the rows per page. The page that now holds the first row on screen is the one shown."""
    if size not in PAGE_SIZES:
        raise RegisterError("Choose one of the page sizes.", 422)
    first_row = (max(page, 1) - 1) * page_size(db)
    save_page_size(db, size)
    state = _state(db, account, _spec(f), saved, first_row // size + 1)
    return reply(request, state, page_rows(db, state), url_mode="replace", focus="size")


@router.post("/transactions/filter/add", response_class=HTMLResponse)
def filter_add(
    request: Request,
    db: DbSession,
    field: Annotated[str, Form()],
    op: Annotated[str, Form()],
    index: Annotated[str, Form()] = "",
    value: Annotated[str, Form()] = "",
    value_to: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    amount_to: Annotated[str, Form()] = "",
    origin: Annotated[str, Form()] = "manual",
    account_id: Annotated[list[str] | None, Form()] = None,
    category_id: Annotated[list[str] | None, Form()] = None,
    f: Opt = None,
    account: OptInt = None,
    saved: OptInt = None,
) -> HTMLResponse:
    """Add a condition, or replace the one at `index`. The rules are in omsync.ledger.filters."""
    spec = _spec(f)
    values = FormValues(
        field=field,
        op=op,
        index=index,
        value=value,
        value_to=value_to,
        amount=amount,
        amount_to=amount_to,
        origin=origin,
    )
    try:
        values.account_ids = _ints(account_id)
        values.category_ids = _ints(category_id)
        condition = condition_from(values)
    except FilterError as exc:
        values.error = str(exc)
        state = _state(db, account, spec, saved)
        return fragment(request, "frag_filter_form", status_code=422, values=values, state=state)
    conditions = list(spec.conditions)
    if index:
        conditions[_position(index, spec)] = condition
    else:
        conditions.append(condition)
    return _changed(request, db, replace(spec, conditions=tuple(conditions)), account, saved)


@router.post("/transactions/filter/remove", response_class=HTMLResponse)
def filter_remove(
    request: Request,
    db: DbSession,
    index: Annotated[int, Form()],
    f: Opt = None,
    account: OptInt = None,
    saved: OptInt = None,
) -> HTMLResponse:
    spec = _spec(f)
    conditions = list(spec.conditions)
    del conditions[_position(str(index), spec)]
    return _changed(request, db, replace(spec, conditions=tuple(conditions)), account, saved)


@router.post("/transactions/filter/match", response_class=HTMLResponse)
def filter_match(
    request: Request,
    db: DbSession,
    match: Annotated[str, Form()],
    f: Opt = None,
    account: OptInt = None,
    saved: OptInt = None,
) -> HTMLResponse:
    spec = _spec(f)
    try:
        changed = replace(spec, match=match)  # type: ignore[arg-type]
    except FilterError as exc:
        raise RegisterError(str(exc), 422) from exc
    return _changed(request, db, changed, account, saved)


@router.get("/transactions/saved-filters/{filter_id}/apply", response_class=HTMLResponse)
def saved_apply(
    request: Request, db: DbSession, filter_id: int, account: int | None = None
) -> HTMLResponse:
    """Show a saved filter's conditions, match, and search, in place of the current ones."""
    return _changed(request, db, _saved(db, filter_id).spec, account, filter_id)


@router.post("/transactions/saved-filters", response_class=HTMLResponse)
def saved_create(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
    f: Opt = None,
    account: OptInt = None,
) -> HTMLResponse:
    spec = _spec(f)
    try:
        batch = save_filter(db, name, spec, idempotency_key=idempotency_key or uuid4().hex)
    except FilterError as exc:
        raise RegisterError(str(exc), 422) from exc
    saved_id = int((batch.summary or {}).get("saved_filter_id", 0))
    return _saved_reply(request, db, batch, spec, account, saved_id)


@router.post("/transactions/saved-filters/{filter_id}/update", response_class=HTMLResponse)
def saved_update(
    request: Request,
    db: DbSession,
    filter_id: int,
    idempotency_key: Annotated[str, Form()] = "",
    f: Opt = None,
    account: OptInt = None,
) -> HTMLResponse:
    spec = _spec(f)
    _saved(db, filter_id)
    try:
        batch = update_filter(db, filter_id, spec, idempotency_key=idempotency_key or uuid4().hex)
    except FilterError as exc:
        return fragment(request, "frag_toast", status_code=422, message=str(exc))
    except StalePreview:
        return fragment(request, "frag_toast", status_code=409, message=CHANGED_ELSEWHERE)
    except BatchError as exc:
        return fragment(request, "frag_toast", status_code=409, message=str(exc))
    return _saved_reply(request, db, batch, spec, account, filter_id)


@router.post("/transactions/saved-filters/{filter_id}/delete", response_class=HTMLResponse)
def saved_delete(
    request: Request,
    db: DbSession,
    filter_id: int,
    idempotency_key: Annotated[str, Form()] = "",
    f: Opt = None,
    account: OptInt = None,
) -> HTMLResponse:
    spec = _spec(f)
    _saved(db, filter_id)
    try:
        batch = delete_filter(db, filter_id, idempotency_key=idempotency_key or uuid4().hex)
    except BatchError as exc:
        return fragment(request, "frag_toast", status_code=409, message=str(exc))
    return _saved_reply(request, db, batch, spec, account, None)


# Helpers


def _spec(text: str | None) -> FilterSpec:
    """The filter in a request. An unreadable one raises RegisterError with a 400."""
    try:
        return decode_filter(text)
    except FilterError as exc:
        raise RegisterError(str(exc), 400) from exc


def _saved(db: Session, filter_id: int) -> SavedFilterInfo:
    try:
        return get_filter(db, filter_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such saved filter") from None


def _state(
    db: Session, account: int | None, spec: FilterSpec, saved: int | None, page: int = 1
) -> RegisterState:
    try:
        return build_state(db, account_id=account, spec=spec, saved_id=saved, page=page)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such account") from None


def _changed(
    request: Request,
    db: Session,
    spec: FilterSpec,
    account: int | None,
    saved: int | None,
) -> HTMLResponse:
    """The register after its filter changed: the regions, the first page of rows, and the URL."""
    state = _state(db, account, spec, saved)
    return reply(request, state, page_rows(db, state))


def _saved_reply(
    request: Request,
    db: Session,
    batch: Batch,
    spec: FilterSpec,
    account: int | None,
    saved: int | None,
) -> HTMLResponse:
    """A save, update, or delete: the regions, and a toast whose undo sends the register state."""
    state = _state(db, account, spec, saved)
    label = str((batch.summary or {}).get("label", "Saved filter changed"))
    return reply(
        request,
        state,
        page_rows(db, state),
        block="frag_saved_applied",
        label=label,
        batch_id=batch.id,
    )


def _position(index: int | str, spec: FilterSpec) -> int:
    try:
        position = int(index)
    except ValueError:
        raise RegisterError("That filter does not exist.", 404) from None
    if not 0 <= position < len(spec.conditions):
        raise RegisterError("That filter does not exist.", 404)
    return position


def _ints(values: list[str] | None) -> list[int]:
    out: list[int] = []
    for text in values or []:
        try:
            out.append(int(text))
        except ValueError:
            raise FilterError("Pick items from the list.") from None
    return out
