"""Keyword routes: the keyword lists, a draft keyword's preview, and the payee tester that explains
which keyword wins.

Each write is one batch, followed in the same request by the automatic step, so categories follow
the keywords at once; the toast names both, and undo reverses both (see applied_toast). The forms
and panels here are also what the page and the triage form build on.
"""

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.ledger import categories_admin as admin
from omsync.ledger.categories_admin import KeywordRow, NothingChanged
from omsync.ledger.categorize import PAYEE_LIST, explain, find_conflicts, is_list_key, list_name
from omsync.ledger.payment_methods import METHOD_LABELS
from omsync.ledger.receipt_vendors import (
    RECEIPT_VENDOR_KEYS,
    costco_food_category,
    set_costco_food_category,
)
from omsync.web.categories_replies import (
    applied_toast,
    block,
    events,
    preview_reply,
    refused,
    whole_number,
)
from omsync.web.deps import get_db

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


# Keywords


@router.get("/categories/keywords", response_class=HTMLResponse)
def keyword_panel(
    request: Request,
    db: DbSession,
    list_key: Annotated[str | None, Query(alias="list")] = None,
    q: str = "",
) -> HTMLResponse:
    """The keyword panel. `q` keeps the keywords whose pattern or category contains it. A search
    swaps only the results region, so this panel is the full page for a reload (see parts.html)."""
    return block(request, "frag_keyword_panel", **keyword_context(db, safe_list(list_key), query=q))


@router.post("/categories/keywords", response_class=HTMLResponse)
def add_keyword(
    request: Request,
    db: DbSession,
    list_key: Annotated[str, Form()] = PAYEE_LIST,
    pattern: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    is_regex: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.add_keyword(
            db,
            list_key,
            pattern,
            whole_number(category_id, "Pick a category."),
            is_regex=bool(is_regex),
            idempotency_key=key,
        )
    except (ValueError, BatchError) as exc:
        form = keyword_form(
            list_key,
            pattern=pattern,
            category_id=category_id,
            is_regex=bool(is_regex),
            error=str(exc),
        )
        return block(
            request,
            "frag_keyword_add",
            status=refused(exc),
            form=form,
            list_key=list_key,
            choices=admin.category_choices(db),
        )
    return block(
        request,
        "frag_keyword_add",
        trigger=events(changed=True),
        form=keyword_form(list_key),
        list_key=list_key,
        choices=admin.category_choices(db),
        **applied_toast(db, batch),
    )


@router.get("/categories/keywords/{keyword_id:int}/edit", response_class=HTMLResponse)
def edit_keyword(request: Request, db: DbSession, keyword_id: int) -> HTMLResponse:
    row = _keyword_or_404(db, keyword_id)
    return block(request, "frag_keyword_edit", **_keyword_edit(db, row))


@router.get("/categories/keywords/{keyword_id:int}/row", response_class=HTMLResponse)
def keyword_row(request: Request, db: DbSession, keyword_id: int) -> HTMLResponse:
    return block(request, "frag_keyword_row", row=_keyword_or_404(db, keyword_id))


@router.post("/categories/keywords/{keyword_id:int}", response_class=HTMLResponse)
def save_keyword(
    request: Request,
    db: DbSession,
    keyword_id: int,
    list_key: Annotated[str, Form()] = PAYEE_LIST,
    pattern: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    is_regex: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.update_keyword(
            db,
            keyword_id,
            idempotency_key=key,
            pattern=pattern,
            is_regex=bool(is_regex),
            category_id=whole_number(category_id, "Pick a category."),
        )
    except NothingChanged:
        return block(request, "frag_keyword_row", row=_keyword_or_404(db, keyword_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such keyword") from None
    except (ValueError, BatchError) as exc:
        form = keyword_form(
            list_key,
            pattern=pattern,
            category_id=category_id,
            is_regex=bool(is_regex),
            error=str(exc),
            key=key,
        )
        return block(
            request,
            "frag_keyword_edit",
            status=refused(exc),
            **_keyword_edit(db, _keyword_or_404(db, keyword_id), form=form),
        )
    return block(
        request,
        "frag_keyword_row",
        trigger=events(changed=True),
        row=_keyword_or_404(db, keyword_id),
        **applied_toast(db, batch),
    )


@router.post("/categories/keywords/{keyword_id:int}/enabled", response_class=HTMLResponse)
def set_keyword_enabled(
    request: Request,
    db: DbSession,
    keyword_id: int,
    enabled: Annotated[str, Form()] = "1",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.set_keyword_enabled(db, keyword_id, enabled == "1", idempotency_key=key)
    except NothingChanged as exc:
        return block(request, "frag_toast", message=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such keyword") from None
    except BatchError as exc:
        return block(request, "frag_toast", status=409, message=str(exc))
    return block(request, "frag_toast", trigger=events(changed=True), **applied_toast(db, batch))


@router.post("/categories/keywords/{keyword_id:int}/delete", response_class=HTMLResponse)
def delete_keyword(
    request: Request,
    db: DbSession,
    keyword_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.delete_keyword(db, keyword_id, idempotency_key=key)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such keyword") from None
    except BatchError as exc:
        return block(request, "frag_toast", status=409, message=str(exc))
    return block(request, "frag_toast", trigger=events(changed=True), **applied_toast(db, batch))


# Preview


RECEIPT_NOTE = (
    "A receipt keyword changes no transaction here. Apply the vendor's receipts again from "
    "Reconcile to fill in the transactions they split."
)


@router.post("/categories/keywords/preview", response_class=HTMLResponse)
def preview_new_keyword(
    request: Request,
    db: DbSession,
    list_key: Annotated[str, Form()] = PAYEE_LIST,
    pattern: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    is_regex: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """The preview of a keyword that would be added."""
    return _preview(request, db, list_key, pattern, category_id, is_regex, None)


@router.post("/categories/keywords/{keyword_id:int}/preview", response_class=HTMLResponse)
def preview_saved_keyword(
    request: Request,
    db: DbSession,
    keyword_id: int,
    pattern: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    is_regex: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """The preview of an edit. The keyword keeps its list and whether it is on."""
    row = _keyword_or_404(db, keyword_id)
    return _preview(request, db, row.list_key, pattern, category_id, is_regex, keyword_id)


def _preview(
    request: Request,
    db: Session,
    list_key: str,
    pattern: str,
    category_id: str,
    is_regex: str,
    keyword_id: int | None,
) -> HTMLResponse:
    try:
        preview = admin.preview_keyword(
            db,
            list_key,
            pattern,
            whole_number(category_id, "Pick a category."),
            is_regex=bool(is_regex),
            keyword_id=keyword_id,
        )
    except ValueError as exc:
        return preview_reply(request, None, error=str(exc))
    if list_key != PAYEE_LIST:
        return preview_reply(request, None, note=RECEIPT_NOTE)
    return preview_reply(request, preview)


# Test a payee


@router.get("/categories/test", response_class=HTMLResponse)
def test_payee(
    request: Request, db: DbSession, payee: str = "", amount: str = "", person: str = ""
) -> HTMLResponse:
    text = payee.strip()
    matches: list[Any] = []
    method = None
    rule = None
    error = ""
    if text:
        try:
            amount_cents = admin.parse_dollars(amount) if amount.strip() else None
            explanation = explain(db, text, amount_cents, with_person=person == "1")
        except ValueError as exc:  # a bad amount, or InvalidKeyword
            error = str(exc)
        else:
            matches = explanation.matches
            method = explanation.method
            rule = None if explanation.rule is None else admin.get_rule(db, explanation.rule.id)
    return block(
        request,
        "frag_payee_results",
        payee=text,
        amount=amount.strip(),
        person=person == "1",
        matches=matches,
        method=None if method is None else METHOD_LABELS[method],
        rule=rule,
        error=error,
        names=admin.category_names(db),
    )


# Helpers


def keyword_form(list_key: str, **values: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "list_key": list_key,
        "pattern": "",
        "category_id": "",
        "is_regex": True,
        "error": "",
        "key": uuid4().hex,
    }
    form.update(values)
    form.setdefault("key", uuid4().hex)
    return form


def keyword_context(db: Session, list_key: str, *, query: str = "") -> dict[str, Any]:
    return {
        "list_key": list_key,
        "lists": list_choices(db),
        "conflicts": find_conflicts(db),
        "query": query,
        "rows": admin.keywords_in_list(db, list_key, query),
        "total": admin.keyword_count(db, list_key),
        "choices": admin.category_choices(db),
        "form": keyword_form(list_key),
        "food": costco_food_category(db) if list_key == "costco" else None,
    }


@router.post("/categories/keywords/costco-food", response_class=HTMLResponse)
def save_costco_food(
    request: Request, db: DbSession, category_id: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Set the category for Costco food items that no keyword matches, or turn it off when blank."""
    chosen = int(category_id) if category_id.strip().isdigit() else None
    error = ""
    try:
        set_costco_food_category(db, chosen)
    except ValueError as exc:
        error = str(exc)
    return block(
        request,
        "frag_costco_food",
        status=422 if error else 200,
        food=costco_food_category(db),
        choices=admin.category_choices(db),
        error=error,
        saved=not error,
    )


def _keyword_edit(
    db: Session, row: KeywordRow, *, form: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The edit row for one keyword. A hidden category stays in the choices, so saving keeps it."""
    choices = admin.category_choices(db)
    if row.category_hidden:
        choices = [(row.category_id, row.category_name), *choices]
    values = form or keyword_form(
        row.list_key,
        pattern=row.pattern,
        category_id=str(row.category_id),
        is_regex=row.is_regex,
    )
    return {"row": row, "form": {**values, "id": row.id}, "choices": choices}


def list_choices(db: Session) -> list[tuple[str, str]]:
    """Payees, each registered vendor, then any other list that has keywords."""
    vendors = RECEIPT_VENDOR_KEYS
    others = [
        key for key in admin.keyword_list_keys(db) if key != PAYEE_LIST and key not in vendors
    ]
    return [(key, list_name(key)) for key in (PAYEE_LIST, *vendors, *others)]


def _keyword_or_404(db: Session, keyword_id: int) -> KeywordRow:
    try:
        return admin.get_keyword(db, keyword_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such keyword") from None


def safe_list(value: str | None) -> str:
    """A list key the page can show. Anything else falls back to payee."""
    if value and is_list_key(value):
        return value
    return PAYEE_LIST
