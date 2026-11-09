"""Rule routes: the rules that set a category when their conditions hold, and their preview.

The form names every condition a rule has, so saving it never drops one. Each write is one batch
from the domain, followed in the same request by the automatic step (see omsync.web.automatic),
which categorizes again so the categories the rules set are current when the reply comes back. The
toast names both, and undo reverses both (see applied_toast and undo_with_automatic).
"""

from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.core.money import format_cents
from omsync.ledger import categories_admin as admin
from omsync.ledger.categories_admin import NothingChanged, RuleDraft, RuleRow
from omsync.ledger.models import RuleOtherSide
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
_FORM_FIELDS = ("paid_by", "below", "at_least", "other_side", "account_id")


@dataclass(frozen=True)
class RuleFields:
    """The rule form as it was submitted. Every value is text, so a bad one is refused with a
    message rather than a type error."""

    paid_by: tuple[str, ...] = ()
    below: str = ""
    at_least: str = ""
    other_side: str = ""
    account_id: str = ""
    to_category_id: str = ""
    idempotency_key: str = ""


def rule_fields(
    paid_by: Annotated[list[str] | None, Form()] = None,
    below: Annotated[str, Form()] = "",
    at_least: Annotated[str, Form()] = "",
    other_side: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    to_category_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> RuleFields:
    return RuleFields(
        paid_by=tuple(paid_by or ()),
        below=below,
        at_least=at_least,
        other_side=other_side,
        account_id=account_id,
        to_category_id=to_category_id,
        idempotency_key=idempotency_key,
    )


Fields = Annotated[RuleFields, Depends(rule_fields)]


# Page fragments


@router.get("/categories/rules", response_class=HTMLResponse)
def rule_panel(request: Request, db: DbSession) -> HTMLResponse:
    return block(request, "frag_rule_panel", **rule_context(db))


@router.post("/categories/rules", response_class=HTMLResponse)
def add_rule(request: Request, db: DbSession, fields: Fields) -> HTMLResponse:
    key = fields.idempotency_key or uuid4().hex
    try:
        batch = admin.add_rule(db, _draft(fields), idempotency_key=key)
    except (ValueError, BatchError) as exc:
        form = _rule_form(**_values(fields), error=str(exc), key=key)
        return block(request, "frag_rule_add", status=refused(exc), form=form, **_pickers(db))
    return block(
        request,
        "frag_rule_add",
        trigger=events(changed=True),
        form=_rule_form(),
        **_pickers(db),
        **applied_toast(db, batch),
    )


@router.get("/categories/rules/{rule_id:int}/edit", response_class=HTMLResponse)
def edit_rule(request: Request, db: DbSession, rule_id: int) -> HTMLResponse:
    return block(request, "frag_rule_edit", **_rule_edit(db, _rule_or_404(db, rule_id)))


@router.get("/categories/rules/{rule_id:int}/row", response_class=HTMLResponse)
def rule_row(request: Request, db: DbSession, rule_id: int) -> HTMLResponse:
    return block(request, "frag_rule_row", **_row_context(db, rule_id))


# Preview: what a rule would change, and writes nothing


@router.post("/categories/rules/preview", response_class=HTMLResponse)
def preview_new_rule(request: Request, db: DbSession, fields: Fields) -> HTMLResponse:
    """The preview of a rule that would be added last."""
    return _preview_reply(request, db, fields, rule_id=None)


@router.post("/categories/rules/{rule_id:int}/preview", response_class=HTMLResponse)
def preview_saved_rule(
    request: Request, db: DbSession, rule_id: int, fields: Fields
) -> HTMLResponse:
    """The preview of an edit: the rule keeps its place and whether it is on."""
    _rule_or_404(db, rule_id)
    return _preview_reply(request, db, fields, rule_id=rule_id)


# Saved rules


@router.post("/categories/rules/{rule_id:int}", response_class=HTMLResponse)
def save_rule(request: Request, db: DbSession, rule_id: int, fields: Fields) -> HTMLResponse:
    key = fields.idempotency_key or uuid4().hex
    stored = _rule_or_404(db, rule_id)
    try:
        batch = admin.update_rule(db, rule_id, _draft(fields), idempotency_key=key)
    except NothingChanged:
        return block(request, "frag_rule_row", **_row_context(db, rule_id))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such rule") from None
    except (ValueError, BatchError) as exc:
        form = _rule_form(**_values(fields), error=str(exc), key=key)
        return block(
            request,
            "frag_rule_edit",
            status=refused(exc),
            **_rule_edit(db, stored, form=form),
        )
    return block(
        request,
        "frag_rule_row",
        trigger=events(changed=True),
        **_row_context(db, rule_id),
        **applied_toast(db, batch),
    )


@router.post("/categories/rules/{rule_id:int}/enabled", response_class=HTMLResponse)
def set_rule_on(
    request: Request,
    db: DbSession,
    rule_id: int,
    enabled: Annotated[str, Form()] = "1",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.set_rule_enabled(db, rule_id, enabled == "1", idempotency_key=key)
    except NothingChanged as exc:
        return block(request, "frag_toast", message=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such rule") from None
    except BatchError as exc:
        return block(request, "frag_toast", status=409, message=str(exc))
    return block(request, "frag_toast", trigger=events(changed=True), **applied_toast(db, batch))


@router.post("/categories/rules/{rule_id:int}/move", response_class=HTMLResponse)
def move_rule_place(
    request: Request,
    db: DbSession,
    rule_id: int,
    direction: Annotated[Literal["up", "down"], Query()],
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Swap the rule with its neighbour. The panel reloads, so every row's buttons are current."""
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.move_rule(db, rule_id, direction, idempotency_key=key)
    except NothingChanged as exc:
        return block(request, "frag_toast", message=str(exc))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such rule") from None
    except BatchError as exc:
        return block(request, "frag_toast", status=409, message=str(exc))
    return block(request, "frag_toast", trigger=events(changed=True), **applied_toast(db, batch))


@router.post("/categories/rules/{rule_id:int}/delete", response_class=HTMLResponse)
def delete_rule_route(
    request: Request,
    db: DbSession,
    rule_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.delete_rule(db, rule_id, idempotency_key=key)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such rule") from None
    except BatchError as exc:
        return block(request, "frag_toast", status=409, message=str(exc))
    return block(request, "frag_toast", trigger=events(changed=True), **applied_toast(db, batch))


# Helpers


def rule_context(db: Session) -> dict[str, Any]:
    return {"rows": admin.rule_rows(db), "form": _rule_form(), **_pickers(db)}


def _draft(fields: RuleFields) -> RuleDraft:
    """The rule the form describes. Text the form cannot read is refused with a message."""
    other, contact_id = _other_side(fields.other_side)
    return RuleDraft(
        to_category_id=whole_number(fields.to_category_id, "Pick the category to set."),
        paid_by=fields.paid_by,
        below_cents=_optional_dollars(fields.below),
        at_least_cents=_optional_dollars(fields.at_least),
        other_side=other,
        contact_id=contact_id,
        account_id=_optional_number(fields.account_id, "Pick an account."),
    )


def _other_side(value: str) -> tuple[str | None, int | None]:
    """The other side the form names: anyone (blank), a person, one of your accounts, or a contact
    written as contact:ID."""
    value = value.strip()
    if not value:
        return None, None
    if value.startswith("contact:"):
        contact = whole_number(value.removeprefix("contact:"), "Pick the contact.")
        return RuleOtherSide.contact.value, contact
    if value in (RuleOtherSide.person.value, RuleOtherSide.owner_account.value):
        return value, None
    raise ValueError("Pick who is on the other side from the list.")


def _optional_number(text: str, message: str) -> int | None:
    return None if not text.strip() else whole_number(text, message)


def _optional_dollars(text: str) -> int | None:
    return None if not text.strip() else admin.parse_dollars(text)


def _values(fields: RuleFields) -> dict[str, Any]:
    """The form's values as submitted, so a refused rule keeps what was typed."""
    return {name: getattr(fields, name) for name in _FORM_FIELDS}


def _rule_form(**values: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "paid_by": (),
        "below": "",
        "at_least": "",
        "other_side": "",
        "account_id": "",
        "to_category_id": "",
        "error": "",
        "key": uuid4().hex,
    }
    form.update(values)
    return form


def _pickers(db: Session, row: RuleRow | None = None) -> dict[str, Any]:
    """What the form offers: the categories a rule may set, the contacts and the accounts. A hidden
    category that a rule already sets stays in the categories, so saving keeps it."""
    choices = admin.category_choices(db)
    if row is not None and row.to_category_hidden:
        choices = [(row.to_category_id, row.to_category_name), *choices]
    return {
        "choices": choices,
        "contacts": admin.contact_choices(db),
        "accounts": admin.account_choices(db),
    }


def _rule_edit(db: Session, row: RuleRow, *, form: dict[str, Any] | None = None) -> dict[str, Any]:
    values = form or _rule_form(
        paid_by=row.paid_by,
        below=_dollars(row.below_cents),
        at_least=_dollars(row.at_least_cents),
        other_side=_other_side_text(row),
        account_id=_text(row.account_id),
        to_category_id=str(row.to_category_id),
    )
    return {"row": row, "form": {**values, "id": row.id}, **_pickers(db, row)}


def _other_side_text(row: RuleRow) -> str:
    """The other side as the form's select names it: a contact as contact:ID."""
    if row.other_side == RuleOtherSide.contact.value:
        return f"contact:{row.contact_id}"
    return row.other_side or ""


def _text(number: int | None) -> str:
    return "" if number is None else str(number)


def _dollars(cents: int | None) -> str:
    return "" if cents is None else format_cents(cents, symbol="")


def _preview_reply(
    request: Request, db: Session, fields: RuleFields, *, rule_id: int | None
) -> HTMLResponse:
    try:
        preview = admin.preview_rule(db, _draft(fields), rule_id=rule_id)
    except ValueError as exc:
        return preview_reply(request, None, error=str(exc))
    return preview_reply(request, preview)


def _row_context(db: Session, rule_id: int) -> dict[str, Any]:
    """One rule's row, and whether it is first or last, which decides its move buttons."""
    row = _rule_or_404(db, rule_id)
    order = [rule.id for rule in admin.rule_rows(db)]
    return {"row": row, "first": order[0] == rule_id, "last": order[-1] == rule_id}


def _rule_or_404(db: Session, rule_id: int) -> RuleRow:
    try:
        return admin.get_rule(db, rule_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such rule") from None
