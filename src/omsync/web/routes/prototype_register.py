"""Routes for the register prototype: the page, inline cell edits, and the split editor.

Every handler returns an HTML fragment rendered from templates/prototype/. Validation
failures return 422 with the form re-rendered; the controller's htmx config swaps those in.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import zip_longest
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from omsync.core.money import format_cents
from omsync.web.prototype.register_store import (
    CATEGORIES,
    EDITABLE_FIELDS,
    RegisterError,
    RegisterStore,
    Transaction,
    amount_left,
    check_splits,
)

router = APIRouter(prefix="/prototype/register")
PAGE = "prototype/register.html"


@dataclass
class LineView:
    amount: str = ""
    category: str = ""
    error: str = ""


@dataclass
class EditorView:
    lines: list[LineView]
    summary: str
    focus_index: int
    left_text: str
    left_invalid: bool


def _store(request: Request) -> RegisterStore:
    return request.app.state.register_store


def _transaction(request: Request, txn_id: int) -> Transaction:
    txn = _store(request).get(txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="No such transaction")
    return txn


def _field(field_name: str) -> str:
    if field_name not in EDITABLE_FIELDS:
        raise HTTPException(status_code=404, detail="Not an editable field")
    return field_name


def _render(
    request: Request,
    block: str,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, PAGE, dict(context), status_code=status_code, block_name=block
    )


def _left(total_cents: int, amounts: Iterable[str]) -> tuple[str, bool]:
    """The "Amount left" text, and whether the amounts could not be read."""
    try:
        return f"Amount left: {format_cents(amount_left(total_cents, amounts))}", False
    except RegisterError:
        return "Amount left: not an amount", True


def _editor(
    txn: Transaction,
    lines: list[LineView],
    summary: str = "",
    focus_index: int = 0,
) -> EditorView:
    text, invalid = _left(txn.amount_cents, [line.amount for line in lines])
    return EditorView(lines, summary, focus_index, text, invalid)


@router.get("", response_class=HTMLResponse)
def register_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        PAGE,
        {"transactions": _store(request).all(), "categories": CATEGORIES},
    )


@router.get("/cell/{txn_id}/{field_name}", response_class=HTMLResponse)
def show_cell(request: Request, txn_id: int, field_name: str) -> HTMLResponse:
    """The display cell. Escape returns here to cancel an edit."""
    txn = _transaction(request, txn_id)
    field_name = _field(field_name)
    return _render(
        request,
        "frag_cell",
        txn_id=txn.id,
        field=field_name,
        value=txn.value_of(field_name),
        autofocus=True,
    )


@router.get("/cell/{txn_id}/{field_name}/edit", response_class=HTMLResponse)
def edit_cell(request: Request, txn_id: int, field_name: str) -> HTMLResponse:
    """The input that replaces a display cell when editing starts."""
    txn = _transaction(request, txn_id)
    field_name = _field(field_name)
    return _render(
        request,
        "frag_edit",
        txn_id=txn.id,
        field=field_name,
        value=txn.value_of(field_name),
        error="",
    )


@router.post("/cell/{txn_id}/{field_name}", response_class=HTMLResponse)
def save_cell(
    request: Request,
    txn_id: int,
    field_name: str,
    value: Annotated[str, Form()] = "",
) -> HTMLResponse:
    txn = _transaction(request, txn_id)
    field_name = _field(field_name)
    try:
        txn.set_text(field_name, value)
    except RegisterError as exc:
        return _render(
            request,
            "frag_edit",
            status_code=422,
            txn_id=txn.id,
            field=field_name,
            value=value,
            error=str(exc),
        )
    return _render(
        request,
        "frag_cell",
        txn_id=txn.id,
        field=field_name,
        value=txn.value_of(field_name),
        autofocus=True,
    )


@router.get("/split/{txn_id}", response_class=HTMLResponse)
def open_split(request: Request, txn_id: int) -> HTMLResponse:
    """The transaction's row group with the split editor open below it."""
    txn = _transaction(request, txn_id)
    editor = _editor(txn, [LineView(), LineView()])
    return _render(request, "frag_group", txn=txn, editor=editor, focus_field=None)


@router.get("/split/{txn_id}/close", response_class=HTMLResponse)
def close_split(request: Request, txn_id: int) -> HTMLResponse:
    txn = _transaction(request, txn_id)
    return _render(request, "frag_group", txn=txn, editor=None, focus_field="category")


@router.get("/split/{txn_id}/line", response_class=HTMLResponse)
def add_split_line(request: Request, txn_id: int) -> HTMLResponse:
    txn = _transaction(request, txn_id)
    return _render(request, "frag_line", txn_id=txn.id, line=LineView(), autofocus=True)


@router.post("/split/{txn_id}/preview", response_class=HTMLResponse)
def preview_split(
    request: Request,
    txn_id: int,
    amount: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    """Recompute "Amount left" for the amounts typed so far."""
    txn = _transaction(request, txn_id)
    text, invalid = _left(txn.amount_cents, amount or [])
    return _render(request, "frag_left", txn_id=txn.id, text=text, invalid=invalid)


@router.post("/split/{txn_id}", response_class=HTMLResponse)
def save_split(
    request: Request,
    txn_id: int,
    amount: Annotated[list[str] | None, Form()] = None,
    category: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    txn = _transaction(request, txn_id)
    amounts = amount or []
    categories = category or []
    check = check_splits(txn.amount_cents, amounts, categories)
    if check.lines is None:
        lines = [
            LineView(amount_text, category_text, check.line_errors.get(index, ""))
            for index, (amount_text, category_text) in enumerate(
                zip_longest(amounts, categories, fillvalue="")
            )
        ]
        focus = min(check.line_errors) if check.line_errors else 0
        editor = _editor(txn, lines, check.summary, focus)
        return _render(
            request, "frag_group", status_code=422, txn=txn, editor=editor, focus_field=None
        )
    txn.set_splits(check.lines)
    return _render(request, "frag_group", txn=txn, editor=None, focus_field="category")
