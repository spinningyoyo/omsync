"""Transaction register fragments, inline edits, split editing, and undo responses."""

import json
from dataclasses import dataclass
from itertools import zip_longest
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch, BatchState
from omsync.core.batches import BatchError, UndoConflict, applied_batch_for_key
from omsync.core.money import format_cents
from omsync.ledger.filters import FilterError, FilterSpec
from omsync.ledger.register import (
    RegisterRow,
    SplitCheck,
    SplitDraft,
    categories,
    check_split_drafts,
    clear_splits,
    edit_memo,
    field_version,
    get_transaction_row,
    open_review_count,
    open_reviews,
    row_version,
    set_category,
    set_splits,
    split_balance,
)
from omsync.reconcile.auto import undo_with_automatic
from omsync.web.deps import get_db
from omsync.web.register_replies import FRAGMENTS, reply, review_items
from omsync.web.register_state import (
    RegisterState,
    build_state,
    category_id_from,
    decode_filter,
    page_rows,
)
from omsync.web.routes.accounts import account_undo_reply, nicknames_undo_reply
from omsync.web.routes.contacts import contacts_undo_reply
from omsync.web.routes.other_side import transaction_undo_reply
from omsync.web.routes.plan import plan_undo_reply
from omsync.web.routes.reconcile import undo_reply as reconcile_undo_reply
from omsync.web.routes.recurring import undo_reply as recurring_undo_reply
from omsync.web.routes.settings_providers import provider_undo_reply
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/transactions.html"
EDITABLE_FIELDS = ("category", "memo")
STALE_MESSAGE = "This changed elsewhere, reload."
UNDO_CONFLICT_MESSAGE = "Can't undo: changed since this was made."
UNDONE_WITH_AUTOMATIC = "Undone, with the automatic step that followed it"
ADD_CATEGORIES_HINT = "Add categories on the Categories page."


@dataclass
class LineView:
    """One split line as the editor shows it. `error` is set when the line was rejected."""

    amount: str = ""
    category_id: int | None = None
    memo: str = ""
    error: str = ""


@dataclass
class EditorView:
    lines: list[LineView]
    summary: str
    focus_index: int
    left_text: str
    left_invalid: bool
    can_save: bool


@router.get("/transactions", response_class=HTMLResponse)
def transactions_page(
    request: Request,
    db: DbSession,
    account: int | None = None,
    f: str | None = None,
    saved: int | None = None,
    page: int = 1,
) -> HTMLResponse:
    """The register. A filter that cannot be read shows the page unfiltered, with a 400 and why."""
    error, status = "", 200
    try:
        spec = decode_filter(f)
    except FilterError as exc:
        spec, error, status = FilterSpec(), str(exc), 400
    state = _state_or_404(db, account, spec, saved, page)
    context = shell_context(request, db, active="transactions", title="Transactions")
    return request.app.state.templates.TemplateResponse(
        request,
        PAGE,
        {
            **context,
            "view": "all",
            "state": state,
            "rows": page_rows(db, state),
            "account": state.account,
            "all_href": state.scope_url,
            "review_count": state.review_count,
            "message": error,
        },
        status_code=status,
    )


@router.post("/transactions/{txn_id}/cell/{field_name}", response_class=HTMLResponse)
def save_cell(
    request: Request,
    db: DbSession,
    txn_id: int,
    field_name: str,
    value: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
    version: Annotated[str, Form()] = "",
    account: int | None = None,
) -> HTMLResponse:
    row = _row_or_404(db, txn_id)
    field_name = _editable(field_name)
    key = idempotency_key or uuid4().hex
    if _is_stale(db, row, field_version(row, field_name), key, version):
        # The cell was rendered from an older view. Keep what was typed, refuse the save, and keep
        # the old token, so a resubmit is refused too until the page reloads.
        return _cell_fragment(
            request,
            db,
            row,
            field_name,
            value=value,
            key=key,
            version=version,
            error=STALE_MESSAGE,
            status_code=409,
        )
    try:
        if _unchanged(row, field_name, value) and applied_batch_for_key(db, key) is None:
            # A change that leaves the field as it was writes no batch and shows no toast. A
            # repeat of a key that already applied still returns that batch, so its toast shows
            # again.
            return _cell_fragment(request, db, row, field_name)
        batch = _save_field(db, row, field_name, value, key)
    except ValueError as exc:
        return _cell_fragment(
            request,
            db,
            row,
            field_name,
            value=value,
            key=key,
            version=version,
            error=str(exc),
            status_code=422,
        )
    except BatchError:
        return _cell_fragment(
            request,
            db,
            row,
            field_name,
            value=value,
            key=key,
            version=version,
            error=STALE_MESSAGE,
            status_code=409,
        )
    return _row_fragment(
        request,
        db,
        "frag_cell_saved",
        row=get_transaction_row(db, txn_id),
        field=field_name,
        label=_label(batch),
        batch_id=batch.id,
        account=account,
    )


@router.get("/transactions/{txn_id}/split", response_class=HTMLResponse)
def open_split(
    request: Request, db: DbSession, txn_id: int, account: int | None = None
) -> HTMLResponse:
    """The row group with the split editor open. An existing split is shown for changing."""
    row = _row_or_404(db, txn_id)
    editor = _editor(row.amount_cents, _initial_lines(row))
    return _editor_fragment(
        request,
        db,
        row,
        editor,
        account,
        status_code=200,
        key=uuid4().hex,
        version=row_version(row),
    )


@router.get("/transactions/{txn_id}/split/close", response_class=HTMLResponse)
def close_split(
    request: Request, db: DbSession, txn_id: int, account: int | None = None
) -> HTMLResponse:
    row = _row_or_404(db, txn_id)
    return _row_fragment(
        request,
        db,
        "frag_group",
        row=row,
        show_account=account is None,
        focus_field="category",
    )


@router.get("/transactions/{txn_id}/split/line", response_class=HTMLResponse)
def add_split_line(request: Request, db: DbSession, txn_id: int) -> HTMLResponse:
    _row_or_404(db, txn_id)
    return _row_fragment(request, db, "frag_split_line", line=LineView(), autofocus=True)


@router.post("/transactions/{txn_id}/split/preview", response_class=HTMLResponse)
def preview_split(
    request: Request,
    db: DbSession,
    txn_id: int,
    amount: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    """Recompute "Amount left" and whether Save is allowed, for the amounts typed so far."""
    row = _row_or_404(db, txn_id)
    balance = split_balance(row.amount_cents, amount or [])
    return _fragment(
        request,
        "frag_amount_left",
        txn_id=row.id,
        text=_left_text(balance.left_cents),
        invalid=balance.left_cents is None,
        can_save=balance.can_save,
    )


@router.post("/transactions/{txn_id}/split", response_class=HTMLResponse)
def save_split(
    request: Request,
    db: DbSession,
    txn_id: int,
    amount: Annotated[list[str] | None, Form()] = None,
    category: Annotated[list[str] | None, Form()] = None,
    memo: Annotated[list[str] | None, Form()] = None,
    idempotency_key: Annotated[str, Form()] = "",
    version: Annotated[str, Form()] = "",
    account: int | None = None,
) -> HTMLResponse:
    row = _row_or_404(db, txn_id)
    key = idempotency_key or uuid4().hex
    views, check = _check_lines(db, row.amount_cents, amount or [], category or [], memo or [])
    if _is_stale(db, row, row_version(row), key, version):
        editor = _editor(row.amount_cents, views, STALE_MESSAGE)
        return _editor_fragment(
            request, db, row, editor, account, status_code=409, key=key, version=version
        )
    if check.lines is None:
        editor = _editor(row.amount_cents, views, check.message)
        return _editor_fragment(
            request, db, row, editor, account, status_code=422, key=key, version=version
        )
    try:
        batch = set_splits(db, row.id, check.lines, idempotency_key=key)
    except ValueError as exc:
        editor = _editor(row.amount_cents, views, str(exc))
        return _editor_fragment(
            request, db, row, editor, account, status_code=422, key=key, version=version
        )
    except BatchError:
        editor = _editor(row.amount_cents, views, STALE_MESSAGE)
        return _editor_fragment(
            request, db, row, editor, account, status_code=409, key=key, version=version
        )
    return _row_fragment(
        request,
        db,
        "frag_group_saved",
        row=get_transaction_row(db, txn_id),
        show_account=account is None,
        focus_field="category",
        label=_label(batch),
        batch_id=batch.id,
        account=account,
    )


@router.post("/transactions/{txn_id}/split/clear", response_class=HTMLResponse)
def remove_split(
    request: Request,
    db: DbSession,
    txn_id: int,
    idempotency_key: Annotated[str, Form()] = "",
    version: Annotated[str, Form()] = "",
    account: int | None = None,
) -> HTMLResponse:
    row = _row_or_404(db, txn_id)
    key = idempotency_key or uuid4().hex
    if _is_stale(db, row, row_version(row), key, version):
        editor = _editor(row.amount_cents, _initial_lines(row), STALE_MESSAGE)
        return _editor_fragment(
            request, db, row, editor, account, status_code=409, key=key, version=version
        )
    try:
        batch = clear_splits(db, row.id, idempotency_key=key)
    except ValueError:
        # Already unsplit, for example after a repeated submit. Show the row as it is.
        return _row_fragment(
            request,
            db,
            "frag_group",
            row=row,
            show_account=account is None,
            focus_field="category",
        )
    except BatchError:
        current = get_transaction_row(db, txn_id)
        editor = _editor(current.amount_cents, _initial_lines(current), STALE_MESSAGE)
        return _editor_fragment(
            request, db, current, editor, account, status_code=409, key=key, version=version
        )
    return _row_fragment(
        request,
        db,
        "frag_group_saved",
        row=get_transaction_row(db, txn_id),
        show_account=account is None,
        focus_field="category",
        label=_label(batch),
        batch_id=batch.id,
        account=account,
    )


@router.post("/batches/{batch_id}/undo", response_class=HTMLResponse)
def undo_batch(
    request: Request,
    db: DbSession,
    batch_id: int,
    account: int | None = None,
    f: str | None = None,
    saved: int | None = None,
    page: int = 1,
    start: str | None = None,
    end: str | None = None,
    plan: str | None = None,
    source: str | None = None,
    contact: int | None = None,
    txn: int | None = None,
) -> HTMLResponse:
    """Undo one batch and say so in the toast, together with the automatic batches that followed
    it (see undo_with_automatic). What re-renders depends on the batch: a register row
    is re-rendered out of band, a bulk edit or a saved filter re-renders the register, a review
    re-renders the review list, a reconcile or receipts batch re-runs the Reconcile results for the
    range in start and end, a contact batch re-renders the Contacts page, the transaction detail, or
    the Reconcile results, by the source the toast was sent from, and a nickname batch re-renders
    the nicknames card of the account in account."""
    if db.get(Batch, batch_id) is None:
        raise HTTPException(status_code=404, detail="No such change")
    try:
        batch, automatic = undo_with_automatic(db, batch_id)
    except UndoConflict:
        return _fragment(request, "frag_toast", status_code=409, message=UNDO_CONFLICT_MESSAGE)
    except BatchError:
        current = db.get(Batch, batch_id)
        message = (
            "Can't undo: already undone."
            if current is not None and current.state is BatchState.undone
            else UNDO_CONFLICT_MESSAGE
        )
        return _fragment(request, "frag_toast", status_code=409, message=message)
    if _is_categories_batch(batch.kind):
        return _categories_undone(request, UNDONE_WITH_AUTOMATIC if automatic else "Undone")
    if batch.kind.startswith(("bulk.", "saved_filter.")):
        return _register_refresh_after_undo(request, db, account, f, saved, page)
    if batch.kind == "review_resolve":
        return _review_refresh_after_undo(request, db)
    if batch.kind.startswith("reconcile.") or batch.kind == "receipts":
        return reconcile_undo_reply(request, db, start, end)
    if batch.kind.startswith("account_nickname."):
        message = UNDONE_WITH_AUTOMATIC if automatic else "Undone"
        return nicknames_undo_reply(request, db, account, message)
    if batch.kind.startswith("contact."):
        message = UNDONE_WITH_AUTOMATIC if automatic else "Undone"
        if source == "reconcile":
            return reconcile_undo_reply(request, db, start, end, automatic=True, message=message)
        if source == "transaction":
            return transaction_undo_reply(request, db, txn, message)
        return contacts_undo_reply(request, db, contact, message)
    if batch.kind.startswith("provider_") or (
        batch.kind.startswith("account.") and source == "providers"
    ):
        return provider_undo_reply(request, db)
    if batch.kind.startswith("account."):
        account_id = (batch.summary or {}).get("account_id")
        return account_undo_reply(request, db, account_id if isinstance(account_id, int) else None)
    if batch.kind.startswith("recurring_"):
        return recurring_undo_reply(request, db)
    if batch.kind.startswith(("budget_", "goal.")):
        return plan_undo_reply(request, db, plan)
    txn_id = (batch.summary or {}).get("transaction_id")
    row = get_transaction_row(db, txn_id) if isinstance(txn_id, int) else None
    return _row_fragment(
        request,
        db,
        "frag_undo",
        row=row,
        show_account=account is None,
        focus_field=_focus_for(batch),
        message="Undone",
    )


def _register_refresh_after_undo(
    request: Request,
    db: Session,
    account: int | None,
    f: str | None,
    saved: int | None,
    page: int,
) -> HTMLResponse:
    """A bulk or saved-filter undo changed rows or saved filters, so the register is re-rendered
    with the state the toast was sent from."""
    try:
        spec = decode_filter(f)
        state = build_state(db, account_id=account, spec=spec, saved_id=saved, page=page)
    except (FilterError, LookupError):
        return _fragment(request, "frag_toast", message="Undone")
    return reply(
        request,
        state,
        page_rows(db, state),
        block="frag_refresh_undo",
        url_mode="none",
        message="Undone",
    )


def _is_categories_batch(kind: str) -> bool:
    """Batches made on the Categories page, and categorize, whose undo changes what it shows."""
    return kind in ("categorize", "keyword_import") or kind.startswith(
        ("category.", "keyword.", "rule.")
    )


def _categories_undone(request: Request, message: str) -> HTMLResponse:
    """The Categories page reloads the regions that show categories, keywords, and rules."""
    response = _fragment(request, "frag_toast", message=message)
    response.headers["HX-Trigger"] = json.dumps({"categories-changed": True})
    return response


def _review_refresh_after_undo(request: Request, db: Session) -> HTMLResponse:
    return _fragment(
        request,
        "frag_review_undo",
        items=review_items(open_reviews(db)),
        review_count=open_review_count(db),
        message="Undone",
    )


# Helpers


def _fragment(
    request: Request, block: str, status_code: int = 200, **context: object
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, context, status_code=status_code, block_name=block
    )


def _row_fragment(
    request: Request, db: Session, block: str, status_code: int = 200, **context: object
) -> HTMLResponse:
    """A fragment that shows rows or split lines, whose category pickers list the categories."""
    return _fragment(request, block, status_code, categories=categories(db), **context)


def _state_or_404(
    db: Session, account: int | None, spec: FilterSpec, saved: int | None, page: int
) -> RegisterState:
    try:
        return build_state(db, account_id=account, spec=spec, saved_id=saved, page=page)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such account") from None


def _editor_fragment(
    request: Request,
    db: Session,
    row: RegisterRow,
    editor: EditorView,
    account: int | None,
    *,
    status_code: int,
    key: str,
    version: str,
) -> HTMLResponse:
    """The split editor. `version` is the token the editor was opened with, not the row's current
    one, so a refused save keeps refusing until the editor is reopened."""
    return _row_fragment(
        request,
        db,
        "frag_editor",
        status_code=status_code,
        row=row,
        show_account=account is None,
        editor=editor,
        key=key,
        clear_key=uuid4().hex,
        version=version,
    )


def _row_or_404(db: Session, txn_id: int) -> RegisterRow:
    try:
        return get_transaction_row(db, txn_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such transaction") from None


def _editable(field_name: str) -> str:
    if field_name not in EDITABLE_FIELDS:
        raise HTTPException(status_code=404, detail="Not an editable field")
    return field_name


def _cell_fragment(
    request: Request,
    db: Session,
    row: RegisterRow,
    field_name: str,
    *,
    value: str | None = None,
    key: str = "",
    version: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    """One editable cell. `value` is what the person entered (a category's id), and None shows the
    stored value. An empty `key` or `version` means a fresh key and the field's current token."""
    hint = ADD_CATEGORIES_HINT if error and field_name == "category" and not categories(db) else ""
    return _row_fragment(
        request,
        db,
        "frag_cell",
        status_code=status_code,
        row=row,
        field=field_name,
        value=value,
        key=key,
        version=version,
        error=error,
        hint=hint,
    )


def _is_stale(db: Session, row: RegisterRow, token: str, key: str, version: str) -> bool:
    """True when the editor's view is older than `token`. A repeat of a key that already applied is
    a replay, not a new decision, so it is never stale: it returns the batch it made before."""
    return version != token and applied_batch_for_key(db, key) is None


def _unchanged(row: RegisterRow, field_name: str, value: str) -> bool:
    if row.is_split and field_name == "category":
        return False  # a split's category is refused with a message, so let the domain say so
    if field_name == "memo":
        return (value.strip() or None) == row.memo
    return category_id_from(value) == row.category_id


def _save_field(db: Session, row: RegisterRow, field_name: str, value: str, key: str) -> Batch:
    if field_name == "memo":
        return edit_memo(db, row.id, value, idempotency_key=key)
    return set_category(db, row.id, category_id_from(value), idempotency_key=key)


def _label(batch: Batch) -> str:
    return str((batch.summary or {}).get("label", "Change saved"))


def _focus_for(batch: Batch) -> str:
    field_name = (batch.summary or {}).get("field")
    if field_name in EDITABLE_FIELDS:
        return str(field_name)
    return "category"  # split changes land on the category, as in the prototype


# Split editor


def _initial_lines(row: RegisterRow) -> list[LineView]:
    if row.splits:
        return [
            LineView(
                amount=format_cents(split.amount_cents, symbol=""),
                category_id=split.category_id,
                memo=split.memo or "",
            )
            for split in row.splits
        ]
    return [LineView(), LineView()]


def _editor(total_cents: int, lines: list[LineView], summary: str = "") -> EditorView:
    balance = split_balance(total_cents, [line.amount for line in lines])
    focus = next((index for index, line in enumerate(lines) if line.error), 0)
    return EditorView(
        lines=lines,
        summary=summary,
        focus_index=focus,
        left_text=_left_text(balance.left_cents),
        left_invalid=balance.left_cents is None,
        can_save=balance.can_save,
    )


def _left_text(left: int | None) -> str:
    if left is None:
        return "Amount left: not an amount"
    return f"Amount left: {format_cents(left)}"


def _check_lines(
    db: Session,
    total_cents: int,
    amounts: list[str],
    picks: list[str],
    memos: list[str],
) -> tuple[list[LineView], SplitCheck]:
    """The submitted form fields as editor lines, with the domain's verdict on them."""
    drafts = [
        SplitDraft(amount_text, _picked(pick), memo_text)
        for amount_text, pick, memo_text in zip_longest(amounts, picks, memos, fillvalue="")
    ]
    check = check_split_drafts(db, total_cents, drafts)
    views = [
        LineView(draft.amount, draft.category_id, draft.memo, error)
        for draft, error in zip(drafts, check.line_errors, strict=True)
    ]
    return views, check


def _picked(text: str) -> int | None:
    """The category picked on a split line. Text that is not an id counts as no pick, so the line
    asks for a category."""
    try:
        return category_id_from(text)
    except ValueError:
        return None
