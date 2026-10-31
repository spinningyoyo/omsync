"""Keyword routes: the keyword lists, and the payee tester that explains which keyword wins.

The forms and panels here are also what the page and the triage form build on.
"""

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.ledger import categories_admin as admin
from omsync.ledger.categories_admin import KeywordRow, NothingChanged
from omsync.ledger.categorize import PAYEE_LISTS, explain, find_conflicts, is_list_key
from omsync.web.categories_replies import block, events, refused, toast, whole_number
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
    list_key: Annotated[str, Form()] = "unique",
    pattern: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    priority: Annotated[str, Form()] = "0",
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
            priority=whole_number(priority, "Priority must be a whole number, such as 10."),
            idempotency_key=key,
        )
    except (ValueError, BatchError) as exc:
        form = keyword_form(
            list_key,
            pattern=pattern,
            category_id=category_id,
            priority=priority,
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
        **toast(batch),
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
    list_key: Annotated[str, Form()] = "unique",
    pattern: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    priority: Annotated[str, Form()] = "0",
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
            priority=whole_number(priority, "Priority must be a whole number, such as 10."),
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
            priority=priority,
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
        **toast(batch),
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
    return block(request, "frag_toast", trigger=events(changed=True), **toast(batch))


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
    return block(request, "frag_toast", trigger=events(changed=True), **toast(batch))


# Test a payee


@router.get("/categories/test", response_class=HTMLResponse)
def test_payee(
    request: Request, db: DbSession, payee: str = "", amount: str = "", person: str = ""
) -> HTMLResponse:
    text = payee.strip()
    matches: list[Any] = []
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
            rule = None if explanation.rule is None else admin.get_rule(db, explanation.rule.id)
    return block(
        request,
        "frag_payee_results",
        payee=text,
        amount=amount.strip(),
        person=person == "1",
        matches=matches,
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
        "priority": "0",
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
    }


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
        priority=str(row.priority),
        is_regex=row.is_regex,
    )
    return {"row": row, "form": {**values, "id": row.id}, "choices": choices}


def list_choices(db: Session) -> list[tuple[str, str]]:
    """Unique, generic, each registered vendor, then any other list that has keywords."""
    vendors = admin.receipt_vendor_keys()
    others = [
        key for key in admin.keyword_list_keys(db) if key not in PAYEE_LISTS and key not in vendors
    ]
    return [(key, key.capitalize()) for key in (*PAYEE_LISTS, *vendors, *others)]


def _keyword_or_404(db: Session, keyword_id: int) -> KeywordRow:
    try:
        return admin.get_keyword(db, keyword_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such keyword") from None


def safe_list(value: str | None) -> str:
    """A list key the page can show. Anything else falls back to unique."""
    if value and is_list_key(value):
        return value
    return "unique"
