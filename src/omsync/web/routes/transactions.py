"""Transaction register fragments, inline edits, split editing, and the register's undo replies."""

from dataclasses import dataclass
from itertools import zip_longest
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import CHANGED_ELSEWHERE, BatchError, applied_batch_for_key
from omsync.core.money import format_cents
from omsync.ledger.filters import FilterError, FilterSpec
from omsync.ledger.register import (
    RegisterRow,
    SplitCheck,
    SplitDraft,
    categories,
    check_split_drafts,
    clear_splits,
    create_tag_on_transaction,
    edit_memo,
    field_version,
    get_transaction_row,
    open_review_count,
    open_reviews,
    row_version,
    set_category,
    set_splits,
    split_balance,
    toggle_split_tag,
    toggle_transaction_tag,
)
from omsync.ledger.tags import create_tag, list_tags
from omsync.web.counterparty_view import load_detail
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.register_replies import FRAGMENTS, reply, review_items
from omsync.web.register_state import (
    RegisterState,
    build_state,
    category_id_from,
    decode_filter,
    page_rows,
)
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/transactions.html"
EDITABLE_FIELDS = ("category",)
ADD_CATEGORIES_HINT = "Add categories on the Categories page."


@dataclass
class LineView:
    """One split line as the editor shows it. `error` is set when the line was rejected."""

    amount: str = ""
    category_id: int | None = None
    memo: str = ""
    error: str = ""
    tag_ids: tuple[int, ...] = ()


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
    sort: str | None = None,
) -> HTMLResponse:
    """The register. A filter that cannot be read shows the page unfiltered, with a 400 and why."""
    error, status = "", 200
    try:
        spec = decode_filter(f)
    except FilterError as exc:
        spec, error, status = FilterSpec(), str(exc), 400
    state = _state_or_404(db, account, spec, saved, page, sort)
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
            error=CHANGED_ELSEWHERE,
            stale=True,
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
            error=CHANGED_ELSEWHERE,
            stale=True,
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
    return _row_fragment(
        request,
        db,
        "frag_split_line",
        line=LineView(),
        autofocus=True,
        all_tags=list_tags(db),
    )


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
    tag: Annotated[list[str] | None, Form()] = None,
    idempotency_key: Annotated[str, Form()] = "",
    version: Annotated[str, Form()] = "",
    account: int | None = None,
) -> HTMLResponse:
    row = _row_or_404(db, txn_id)
    key = idempotency_key or uuid4().hex
    views, check = _check_lines(
        db, row.amount_cents, amount or [], category or [], memo or [], tag or []
    )
    if _is_stale(db, row, row_version(row), key, version):
        editor = _editor(row.amount_cents, views, CHANGED_ELSEWHERE)
        return _editor_fragment(
            request, db, row, editor, account, status_code=409, key=key, version=version
        )
    if check.lines is None:
        editor = _editor(row.amount_cents, views, check.message)
        return _editor_fragment(
            request, db, row, editor, account, status_code=422, key=key, version=version
        )
    try:
        batch = set_splits(db, row.id, check.lines, tags=check.line_tags, idempotency_key=key)
    except ValueError as exc:
        editor = _editor(row.amount_cents, views, str(exc))
        return _editor_fragment(
            request, db, row, editor, account, status_code=422, key=key, version=version
        )
    except BatchError:
        editor = _editor(row.amount_cents, views, CHANGED_ELSEWHERE)
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
        editor = _editor(row.amount_cents, _initial_lines(row), CHANGED_ELSEWHERE)
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
        editor = _editor(current.amount_cents, _initial_lines(current), CHANGED_ELSEWHERE)
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


def register_refresh_undo_reply(
    request: Request,
    db: Session,
    account: int | None,
    f: str | None,
    saved: int | None,
    page: int,
    sort: str | None = None,
) -> HTMLResponse:
    """A bulk or saved-filter undo changed rows or saved filters, so the register is re-rendered
    with the state the toast was sent from."""
    try:
        spec = decode_filter(f)
        state = build_state(db, account_id=account, spec=spec, saved_id=saved, page=page, sort=sort)
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


def review_undo_reply(request: Request, db: Session) -> HTMLResponse:
    """After an undo of a review decision: the review list as it now is."""
    return _fragment(
        request,
        "frag_review_undo",
        items=review_items(open_reviews(db)),
        review_count=open_review_count(db),
        message="Undone",
    )


def row_undo_reply(
    request: Request, db: Session, batch: Batch, account: int | None
) -> HTMLResponse:
    """After an undo of an edit in the register: the row as it now is."""
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


# Helpers


def _fragment(
    request: Request, block: str, status_code: int = 200, **context: object
) -> HTMLResponse:
    return render_block(request, FRAGMENTS, block, status=status_code, **context)


def _row_fragment(
    request: Request, db: Session, block: str, status_code: int = 200, **context: object
) -> HTMLResponse:
    """A fragment that shows rows or split lines, whose category pickers list the categories."""
    return _fragment(request, block, status_code, categories=categories(db), **context)


def _state_or_404(
    db: Session,
    account: int | None,
    spec: FilterSpec,
    saved: int | None,
    page: int,
    sort: str | None = None,
) -> RegisterState:
    try:
        return build_state(db, account_id=account, spec=spec, saved_id=saved, page=page, sort=sort)
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
        all_tags=list_tags(db),
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
    stale: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    """One editable cell. `value` is what the person entered (a category's id), and None shows the
    stored value. An empty `key` or `version` means a fresh key and the field's current token.
    `stale` marks a save refused because the cell is out of date, which offers a reload."""
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
        stale=stale,
    )


def _is_stale(db: Session, row: RegisterRow, token: str, key: str, version: str) -> bool:
    """True when the editor's view is older than `token`. A repeat of a key that already applied is
    a replay, not a new decision, so it is never stale: it returns the batch it made before."""
    return version != token and applied_batch_for_key(db, key) is None


def _unchanged(row: RegisterRow, field_name: str, value: str) -> bool:
    if row.is_split and field_name == "category":
        return False  # a split's category is refused with a message, so let the domain say so
    return category_id_from(value) == row.category_id


def _save_field(db: Session, row: RegisterRow, field_name: str, value: str, key: str) -> Batch:
    return set_category(db, row.id, category_id_from(value), idempotency_key=key)


def _label(batch: Batch) -> str:
    return str((batch.summary or {}).get("label", "Change saved"))


def _focus_for(batch: Batch) -> str:
    field_name = (batch.summary or {}).get("field")
    if field_name in EDITABLE_FIELDS:
        return str(field_name)
    return "category"  # split changes land on the category


# Split editor


def _initial_lines(row: RegisterRow) -> list[LineView]:
    if row.splits:
        return [
            LineView(
                amount=format_cents(split.amount_cents, symbol=""),
                category_id=split.category_id,
                memo=split.memo or "",
                tag_ids=tuple(tag.id for tag in split.tags),
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
    tag_values: list[str],
) -> tuple[list[LineView], SplitCheck]:
    """The submitted form fields as editor lines, with the domain's verdict on them."""
    groups = _tag_groups(db, tag_values)
    drafts = [
        SplitDraft(amount_text, _picked(pick), memo_text, tag_ids)
        for amount_text, pick, memo_text, tag_ids in zip_longest(
            amounts, picks, memos, groups, fillvalue=""
        )
    ]
    check = check_split_drafts(db, total_cents, drafts)
    views = [
        LineView(draft.amount, draft.category_id, draft.memo, error, draft.tag_ids)
        for draft, error in zip(drafts, check.line_errors, strict=True)
    ]
    return views, check


def _tag_groups(db: Session, values: list[str]) -> list[tuple[int, ...]]:
    """The tags of each split line. The editor submits a "-" before each line's tag ids, so the
    ids that follow it belong to that line. A value that is not the id of a tag is dropped."""
    known = {tag.id for tag in list_tags(db)}
    groups: list[list[int]] = []
    for value in values:
        if value == "-":
            groups.append([])
        elif groups and value.isdigit() and int(value) in known:
            groups[-1].append(int(value))
    return [tuple(dict.fromkeys(group)) for group in groups]


def _picked(text: str) -> int | None:
    """The category picked on a split line. Text that is not an id counts as no pick, so the line
    asks for a category."""
    try:
        return category_id_from(text)
    except ValueError:
        return None


TAG_POPOVER_TEMPLATE = "transactions/tag_popover.html"
MEMO_POPOVER_TEMPLATE = "transactions/memo_popover.html"
DETAIL_FRAGMENTS = "transactions/detail_fragments.html"
TAG_GONE = "That tag no longer exists."


@router.get("/transactions/{txn_id}/tags/popover", response_class=HTMLResponse)
def get_tag_popover(
    request: Request, db: DbSession, txn_id: int, account: int | None = None
) -> HTMLResponse:
    row = _row_or_404(db, txn_id)
    return request.app.state.templates.TemplateResponse(
        request,
        TAG_POPOVER_TEMPLATE,
        {"row": row, "all_tags": list_tags(db), "account": account},
    )


@router.get("/transactions/{txn_id}/memo/popover", response_class=HTMLResponse)
def get_memo_popover(
    request: Request, db: DbSession, txn_id: int, account: int | None = None
) -> HTMLResponse:
    row = _row_or_404(db, txn_id)
    return request.app.state.templates.TemplateResponse(
        request,
        MEMO_POPOVER_TEMPLATE,
        {"row": row, "account": account},
    )


@router.post("/transactions/{txn_id}/memo", response_class=HTMLResponse)
def post_transaction_memo(
    request: Request,
    db: DbSession,
    txn_id: int,
    memo: Annotated[str, Form()] = "",
    account: int | None = None,
) -> HTMLResponse:
    """Save the memo from its popover. A memo that is as it was changes nothing and shows no
    toast."""
    row = _row_or_404(db, txn_id)
    if (memo.strip() or None) == row.memo:
        return _row_fragment(request, db, "frag_group", row=row, show_account=account is None)
    batch = edit_memo(db, row.id, memo, idempotency_key=uuid4().hex)
    return _row_fragment(
        request,
        db,
        "frag_group_saved",
        row=get_transaction_row(db, txn_id),
        show_account=account is None,
        label=_label(batch),
        batch_id=batch.id,
        account=account,
    )


@router.post("/transactions/{txn_id}/tags/toggle/{tag_id}", response_class=HTMLResponse)
def toggle_tag_route(
    request: Request,
    db: DbSession,
    txn_id: int,
    tag_id: int,
    from_detail: bool = False,
    account: int | None = None,
) -> HTMLResponse:
    row = _row_or_404(db, txn_id)
    batch = toggle_transaction_tag(db, row.id, tag_id, idempotency_key=uuid4().hex)
    return _tags_reply(request, db, txn_id, batch, TAG_GONE, from_detail, account)


@router.post("/transactions/{txn_id}/splits/{split_id}/tags/toggle/{tag_id}")
def toggle_split_tag_route(
    request: Request, db: DbSession, txn_id: int, split_id: int, tag_id: int
) -> HTMLResponse:
    """Put a tag on a split line of the transaction detail, or take it off."""
    row = _row_or_404(db, txn_id)
    if split_id not in {split.id for split in row.splits}:
        raise HTTPException(status_code=404, detail="No such split")
    batch = toggle_split_tag(db, split_id, tag_id, idempotency_key=uuid4().hex)
    return _tags_reply(request, db, txn_id, batch, TAG_GONE, True, None)


@router.post("/transactions/{txn_id}/tags/create", response_class=HTMLResponse)
def create_and_assign_tag_route(
    request: Request,
    db: DbSession,
    txn_id: int,
    name: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    from_detail: bool = False,
    account: int | None = None,
) -> HTMLResponse:
    """Create a tag and put it on the transaction, in one batch."""
    row = _row_or_404(db, txn_id)
    try:
        batch = create_tag_on_transaction(db, row.id, name, color, idempotency_key=uuid4().hex)
    except ValueError as exc:
        return _tags_reply(request, db, txn_id, None, str(exc), from_detail, account, status=422)
    return _tags_reply(request, db, txn_id, batch, "", from_detail, account)


@router.post("/tags/split-picker", response_class=HTMLResponse)
async def create_tag_for_split_line(
    request: Request, db: DbSession, picker: str = ""
) -> HTMLResponse:
    """Create a tag from the picker of a split line, and tick it there. The line is not saved
    yet, so nothing is put on it. The reply is that line's picker, as it was with the new tag."""
    form = await request.form()
    uid = picker or str(form.get("picker", ""))
    items = [(k, v) for k, v in form.multi_items() if isinstance(v, str)]
    ticked = _picker_ticked(items, uid)
    name = str(form.get(f"new_name_{uid}") or form.get("new_name", ""))
    color = str(form.get(f"new_color_{uid}", "")) or next(
        (v for k, v in items if k.startswith("new_color_")), ""
    )
    label, batch_id, status = "", None, 200
    try:
        batch = create_tag(db, name, color, idempotency_key=uuid4().hex)
    except ValueError as exc:
        label, status = str(exc), 422
    else:
        label, batch_id = _label(batch), batch.id
        ticked.append(int((batch.summary or {})["tag_id"]))
    return _fragment(
        request,
        "frag_split_tags",
        status_code=status,
        tags=list_tags(db),
        selected=ticked,
        uid=uid,
        label=label,
        batch_id=batch_id,
    )


def _picker_ticked(items: list[tuple[str, str]], uid: str) -> list[int]:
    """The tag IDs ticked in the picker named `uid`. An enclosing split form sends every line's
    picker in order, each starting with `picker=<uid>` before its `tag` checkboxes."""
    current: str | None = None
    seen_picker = False
    ticked: list[int] = []
    for key, value in items:
        if key == "picker":
            current = value
            seen_picker = True
        elif key == "tag" and value.isdigit() and (not seen_picker or current == uid):
            ticked.append(int(value))
    return list(dict.fromkeys(ticked))


def _tags_reply(
    request: Request,
    db: Session,
    txn_id: int,
    batch: Batch | None,
    message: str,
    from_detail: bool,
    account: int | None,
    *,
    status: int = 200,
) -> HTMLResponse:
    """The transaction as it now is, with a toast. A tag change that wrote nothing has `message`
    and no Undo."""
    label = _label(batch) if batch else message
    batch_id = batch.id if batch else None
    if from_detail:
        return render_block(
            request,
            DETAIL_FRAGMENTS,
            "frag_saved",
            status=status,
            detail=load_detail(db, txn_id),
            message=label,
            batch_id=batch_id,
            txn_id=txn_id,
            all_tags=list_tags(db),
        )
    return _row_fragment(
        request,
        db,
        "frag_group_saved",
        status_code=status,
        row=get_transaction_row(db, txn_id),
        show_account=account is None,
        label=label,
        batch_id=batch_id,
        account=account,
    )
