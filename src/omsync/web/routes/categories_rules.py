"""Amount rule routes: the rules that send a small payee match to another category."""

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.core.money import format_cents
from omsync.ledger import categories_admin as admin
from omsync.ledger.categories_admin import AmountRuleRow, NothingChanged
from omsync.web.categories_replies import block, events, refused, toast, whole_number
from omsync.web.deps import get_db

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/categories/amount-rules", response_class=HTMLResponse)
def rule_panel(request: Request, db: DbSession) -> HTMLResponse:
    return block(request, "frag_rule_panel", **rule_context(db))


@router.post("/categories/amount-rules", response_class=HTMLResponse)
def add_amount_rule(
    request: Request,
    db: DbSession,
    list_key: Annotated[str, Form()] = "generic",
    from_category_id: Annotated[str, Form()] = "",
    below: Annotated[str, Form()] = "",
    to_category_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.add_amount_rule(
            db,
            list_key,
            whole_number(from_category_id, "Pick the category to change from."),
            admin.parse_dollars(below),
            whole_number(to_category_id, "Pick the category to change to."),
            idempotency_key=key,
        )
    except (ValueError, BatchError) as exc:
        form = _rule_form(
            list_key=list_key,
            from_category_id=from_category_id,
            below=below,
            to_category_id=to_category_id,
            error=str(exc),
        )
        return block(
            request,
            "frag_rule_add",
            status=refused(exc),
            form=form,
            choices=admin.category_choices(db),
        )
    return block(
        request,
        "frag_rule_add",
        trigger=events(changed=True),
        form=_rule_form(list_key=list_key),
        choices=admin.category_choices(db),
        **toast(batch),
    )


@router.get("/categories/amount-rules/{rule_id:int}/edit", response_class=HTMLResponse)
def edit_amount_rule(request: Request, db: DbSession, rule_id: int) -> HTMLResponse:
    return block(request, "frag_rule_edit", **_rule_edit(db, _rule_or_404(db, rule_id)))


@router.get("/categories/amount-rules/{rule_id:int}/row", response_class=HTMLResponse)
def amount_rule_row(request: Request, db: DbSession, rule_id: int) -> HTMLResponse:
    return block(request, "frag_rule_row", row=_rule_or_404(db, rule_id))


@router.post("/categories/amount-rules/{rule_id:int}", response_class=HTMLResponse)
def save_amount_rule(
    request: Request,
    db: DbSession,
    rule_id: int,
    list_key: Annotated[str, Form()] = "generic",
    from_category_id: Annotated[str, Form()] = "",
    below: Annotated[str, Form()] = "",
    to_category_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.update_amount_rule(
            db,
            rule_id,
            idempotency_key=key,
            list_key=list_key,
            from_category_id=whole_number(from_category_id, "Pick the category to change from."),
            below_cents=admin.parse_dollars(below),
            to_category_id=whole_number(to_category_id, "Pick the category to change to."),
        )
    except NothingChanged:
        return block(request, "frag_rule_row", row=_rule_or_404(db, rule_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such amount rule") from None
    except (ValueError, BatchError) as exc:
        form = _rule_form(
            list_key=list_key,
            from_category_id=from_category_id,
            below=below,
            to_category_id=to_category_id,
            error=str(exc),
            key=key,
        )
        return block(
            request,
            "frag_rule_edit",
            status=refused(exc),
            **_rule_edit(db, _rule_or_404(db, rule_id), form=form),
        )
    return block(
        request,
        "frag_rule_row",
        trigger=events(changed=True),
        row=_rule_or_404(db, rule_id),
        **toast(batch),
    )


@router.post("/categories/amount-rules/{rule_id:int}/enabled", response_class=HTMLResponse)
def set_amount_rule_enabled(
    request: Request,
    db: DbSession,
    rule_id: int,
    enabled: Annotated[str, Form()] = "1",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.set_amount_rule_enabled(db, rule_id, enabled == "1", idempotency_key=key)
    except NothingChanged as exc:
        return block(request, "frag_toast", message=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such amount rule") from None
    except BatchError as exc:
        return block(request, "frag_toast", status=409, message=str(exc))
    return block(request, "frag_toast", trigger=events(changed=True), **toast(batch))


@router.post("/categories/amount-rules/{rule_id:int}/delete", response_class=HTMLResponse)
def delete_amount_rule(
    request: Request,
    db: DbSession,
    rule_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.delete_amount_rule(db, rule_id, idempotency_key=key)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such amount rule") from None
    except BatchError as exc:
        return block(request, "frag_toast", status=409, message=str(exc))
    return block(request, "frag_toast", trigger=events(changed=True), **toast(batch))


# Helpers


def _rule_form(**values: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "list_key": "generic",
        "from_category_id": "",
        "below": "",
        "to_category_id": "",
        "error": "",
        "key": uuid4().hex,
    }
    form.update(values)
    return form


def rule_context(db: Session) -> dict[str, Any]:
    return {
        "rows": admin.amount_rules(db),
        "choices": admin.category_choices(db),
        "form": _rule_form(),
    }


def _rule_edit(
    db: Session, row: AmountRuleRow, *, form: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The edit row for one amount rule. A hidden category stays in the choices, so saving keeps
    it."""
    choices = admin.category_choices(db)
    for category_id, name, hidden in (
        (row.from_category_id, row.from_category_name, row.from_category_hidden),
        (row.to_category_id, row.to_category_name, row.to_category_hidden),
    ):
        if hidden:
            choices = [(category_id, name), *choices]
    values = form or _rule_form(
        list_key=row.list_key,
        from_category_id=str(row.from_category_id),
        below=format_cents(row.below_cents, symbol=""),
        to_category_id=str(row.to_category_id),
    )
    return {"row": row, "form": {**values, "id": row.id}, "choices": choices}


def _rule_or_404(db: Session, rule_id: int) -> AmountRuleRow:
    try:
        return admin.get_amount_rule(db, rule_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such amount rule") from None
