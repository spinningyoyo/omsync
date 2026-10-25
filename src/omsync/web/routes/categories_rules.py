"""Rule routes: the rules that set a category when their conditions hold, and their preview.

The form names every condition a rule has, so saving it never drops one. Each write is one batch
from the domain, followed in the same request by the automatic step (see omsync.web.automatic),
which categorizes again so the categories the rules set are current when the reply comes back. The
toast names both, and undo reverses both (see undo_with_automatic).
"""

from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError
from omsync.core.money import format_cents
from omsync.ledger import categories_admin as admin
from omsync.ledger.categories_admin import NothingChanged, RuleDraft, RuleRow
from omsync.ledger.models import RuleOtherSide
from omsync.web.automatic import run_automatic, with_automatic
from omsync.web.categories_replies import block, events, refused, toast, whole_number
from omsync.web.deps import get_db

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PREVIEW_LIMIT = 50
_FORM_FIELDS = ("list_key", "from_category_id", "below", "at_least", "other_side", "account_id")


@dataclass(frozen=True)
class RuleFields:
    """The rule form as it was submitted. Every value is text, so a bad one is refused with a
    message rather than a type error."""

    list_key: str = ""
    from_category_id: str = ""
    below: str = ""
    at_least: str = ""
    other_side: str = ""
    account_id: str = ""
    to_category_id: str = ""
    idempotency_key: str = ""


def rule_fields(
    list_key: Annotated[str, Form()] = "",
    from_category_id: Annotated[str, Form()] = "",
    below: Annotated[str, Form()] = "",
    at_least: Annotated[str, Form()] = "",
    other_side: Annotated[str, Form()] = "",
    account_id: Annotated[str, Form()] = "",
    to_category_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> RuleFields:
    return RuleFields(
        list_key=list_key,
        from_category_id=from_category_id,
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
        **_saved_toast(db, batch),
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
        **_saved_toast(db, batch),
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
    return block(request, "frag_toast", trigger=events(changed=True), **_saved_toast(db, batch))


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
    return block(request, "frag_toast", trigger=events(changed=True), **_saved_toast(db, batch))


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
    return block(request, "frag_toast", trigger=events(changed=True), **_saved_toast(db, batch))


# Helpers


def rule_context(db: Session) -> dict[str, Any]:
    return {"rows": admin.rule_rows(db), "form": _rule_form(), **_pickers(db)}


def _draft(fields: RuleFields) -> RuleDraft:
    """The rule the form describes. Text the form cannot read is refused with a message."""
    other, contact_id = _other_side(fields.other_side)
    return RuleDraft(
        to_category_id=whole_number(fields.to_category_id, "Pick the category to set."),
        from_category_id=_optional_number(fields.from_category_id, "Pick the category to read."),
        list_key=fields.list_key or None,
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


def _values(fields: RuleFields) -> dict[str, str]:
    """The form's values as submitted, so a refused rule keeps what was typed."""
    return {name: getattr(fields, name) for name in _FORM_FIELDS}


def _rule_form(**values: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "list_key": "",
        "from_category_id": "",
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
    """What the form offers: the categories a rule may name, the contacts and the accounts. A hidden
    category that a rule already names stays in the categories, so saving keeps it."""
    choices = admin.category_choices(db)
    if row is not None:
        for category_id, name, hidden in (
            (row.from_category_id, row.from_category_name, row.from_category_hidden),
            (row.to_category_id, row.to_category_name, row.to_category_hidden),
        ):
            if hidden and category_id is not None and name is not None:
                choices = [(category_id, name), *choices]
    return {
        "choices": choices,
        "contacts": admin.contact_choices(db),
        "accounts": admin.account_choices(db),
    }


def _rule_edit(db: Session, row: RuleRow, *, form: dict[str, Any] | None = None) -> dict[str, Any]:
    values = form or _rule_form(
        list_key=row.list_key or "",
        from_category_id=_text(row.from_category_id),
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
        return block(request, "frag_rule_preview", status=422, error=str(exc), count=0, moves=())
    return block(
        request,
        "frag_rule_preview",
        error="",
        count=preview.count,
        moves=preview.changes[:PREVIEW_LIMIT],
    )


def _row_context(db: Session, rule_id: int) -> dict[str, Any]:
    """One rule's row, and whether it is first or last, which decides its move buttons."""
    row = _rule_or_404(db, rule_id)
    order = [rule.id for rule in admin.rule_rows(db)]
    return {"row": row, "first": order[0] == rule_id, "last": order[-1] == rule_id}


def _saved_toast(db: Session, batch: Batch) -> dict[str, Any]:
    """The toast for a rule change, after the automatic step has run on it. It says what the step
    categorized, and undo reverses the change together with that step."""
    result = run_automatic(db, batch.id)
    label = with_automatic(str((batch.summary or {}).get("label", "Change saved")), result)
    if result.problem is None and not result.categorized:
        label = f"{label.rstrip('.')}. No transaction's category changed."
    return toast(batch, label=label)


def _rule_or_404(db: Session, rule_id: int) -> RuleRow:
    try:
        return admin.get_rule(db, rule_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such rule") from None
