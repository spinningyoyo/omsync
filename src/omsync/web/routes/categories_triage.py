"""Triage, categorize, and keyword import: the ways unclassified payees and receipt items become
keywords and then categories.

Adding a keyword and applying categorization are separate steps. Categorize and import both
preview first, and an apply is refused when the data moved on since its preview.
"""

import json
import re
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError, applied_batch_for_key
from omsync.core.settings import Settings
from omsync.ledger import categories_admin as admin
from omsync.ledger.categorize import InvalidKeyword, apply_categorize, plan_categorize
from omsync.ledger.keyword_import import (
    KeywordFileError,
    apply_keyword_import,
    parse_keyword_file,
    parse_keyword_mapping,
    plan_keyword_import,
)
from omsync.web.categories_replies import block, events, refused, toast, whole_number
from omsync.web.deps import get_db
from omsync.web.routes.categories_keywords import keyword_form, safe_list

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
STALE = "This changed elsewhere. Preview again."
OUT_OF_DATE = "The import is out of date. Choose the file again."
MAX_UPLOAD_BYTES = 1024 * 1024
_SLOT = re.compile(r"[a-z0-9_-]{1,60}")


# Triage


@router.get("/categories/triage", response_class=HTMLResponse)
def triage(request: Request, db: DbSession) -> HTMLResponse:
    return block(request, "frag_triage", **triage_context(request, db))


@router.get("/categories/triage/form", response_class=HTMLResponse)
def triage_form(
    request: Request,
    db: DbSession,
    kind: str = "payee",
    text: str = "",
    list_key: str = "",
    slot: str = "",
) -> HTMLResponse:
    slot = _slot(slot)
    target_list = "unique" if kind == "payee" else safe_list(list_key)
    form = keyword_form(target_list, pattern=admin.suggest_pattern(text), is_regex=True)
    return block(
        request,
        "frag_triage_form",
        form=form,
        kind=kind,
        text=text,
        slot=slot,
        list_key=target_list,
        choices=admin.category_choices(db),
    )


@router.post("/categories/triage/keywords", response_class=HTMLResponse)
def triage_add_keyword(
    request: Request,
    db: DbSession,
    kind: Annotated[str, Form()] = "payee",
    text: Annotated[str, Form()] = "",
    slot: Annotated[str, Form()] = "",
    list_key: Annotated[str, Form()] = "unique",
    pattern: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    is_regex: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    slot = _slot(slot)
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
            key=key,
        )
        return block(
            request,
            "frag_triage_form",
            status=refused(exc),
            form=form,
            kind=kind,
            text=text,
            slot=slot,
            list_key=list_key,
            choices=admin.category_choices(db),
        )
    return block(
        request,
        "frag_triage_done",
        trigger=events(changed=True),
        slot=slot,
        list_key=list_key,
        **toast(batch),
    )


# Categorize preview


@router.get("/categories/categorize", response_class=HTMLResponse)
def categorize_preview(request: Request, db: DbSession) -> HTMLResponse:
    return block(request, "frag_categorize", **_categorize_context(db))


@router.post("/categories/categorize/apply", response_class=HTMLResponse)
def categorize_apply(
    request: Request,
    db: DbSession,
    revision: Annotated[int | None, Form()] = None,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        plan = plan_categorize(db)
    except InvalidKeyword as exc:
        return block(request, "frag_categorize", status=422, **_categorize_context(db, str(exc)))
    if applied_batch_for_key(db, key) is None:
        if plan.preview is None:
            return block(
                request,
                "frag_categorize",
                status=422,
                **_categorize_context(db, "Nothing to categorize."),
            )
        if plan.preview.base_revision != revision:
            return block(request, "frag_categorize", status=409, **_categorize_context(db, STALE))
    try:
        batch = apply_categorize(db, plan, idempotency_key=key)
    except BatchError as exc:
        return block(
            request, "frag_categorize", status=refused(exc), **_categorize_context(db, str(exc))
        )
    changed = int((batch.summary or {}).get("changed", 0))
    label = f"Categorized {changed} {'transaction' if changed == 1 else 'transactions'}"
    return block(
        request,
        "frag_categorize_done",
        trigger=events(changed=True, close=True),
        message=label,
        **toast(batch, label=label),
    )


# Keyword import


@router.post("/categories/import/preview", response_class=HTMLResponse)
def import_preview(
    request: Request,
    db: DbSession,
    file: Annotated[UploadFile, File()],
    list_key: Annotated[str, Form()] = "unique",
) -> HTMLResponse:
    try:
        mapping, warnings = _read_upload(file)
        plan = plan_keyword_import(db, mapping, list_key=list_key)
    except (KeywordFileError, ValueError) as exc:
        return block(request, "frag_message", status=422, message=str(exc))
    return block(
        request,
        "frag_import_preview",
        warnings=warnings,
        plan=plan,
        list_key=list_key,
        mapping_json=json.dumps(mapping),
        revision=plan.preview.base_revision,
        key=uuid4().hex,
    )


@router.post("/categories/import/apply", response_class=HTMLResponse)
def import_apply(
    request: Request,
    db: DbSession,
    mapping_json: Annotated[str, Form()] = "",
    list_key: Annotated[str, Form()] = "unique",
    revision: Annotated[int | None, Form()] = None,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        mapping = parse_keyword_mapping(mapping_json)
    except KeywordFileError:
        return block(request, "frag_message", status=422, message=OUT_OF_DATE)
    try:
        plan = plan_keyword_import(db, mapping, list_key=list_key)
    except (KeywordFileError, ValueError) as exc:
        return block(request, "frag_message", status=422, message=str(exc))
    if applied_batch_for_key(db, key) is None and plan.preview.base_revision != revision:
        return block(request, "frag_message", status=409, message=STALE)
    try:
        batch = apply_keyword_import(db, plan, idempotency_key=key)
    except BatchError as exc:
        return block(request, "frag_message", status=refused(exc), message=str(exc))
    count = (batch.summary or {}).get("new_keywords", 0)
    message = f"Imported {count} {'keyword' if count == 1 else 'keywords'} to {list_key}."
    return block(
        request,
        "frag_import_done",
        trigger=events(changed=True),
        message=message,
        **toast(batch, label=message),
    )


# Helpers


def triage_context(request: Request, db: Session) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    vendors = [
        {
            "key": key,
            "label": key.capitalize(),
            "unknown": admin.unknown_receipt_items(db, settings, key),
            "has_files": admin.has_receipt_files(settings, key),
        }
        for key in admin.receipt_vendor_keys()
    ]
    return {
        "payees": admin.uncategorized_payees(db),
        "vendors": vendors,
        "choices": admin.category_choices(db),
    }


def _categorize_context(db: Session, error: str = "") -> dict[str, Any]:
    """The preview as it stands now, with the revision that Apply will send back."""
    try:
        plan = plan_categorize(db)
    except InvalidKeyword as exc:
        return {"plan": None, "error": str(exc), "revision": None, "key": uuid4().hex}
    revision = plan.preview.base_revision if plan.preview is not None else None
    return {"plan": plan, "error": error, "revision": revision, "key": uuid4().hex}


def _slot(value: str) -> str:
    if not _SLOT.fullmatch(value or ""):
        raise HTTPException(status_code=400, detail="Bad form slot")
    return value


def _read_upload(file: UploadFile) -> tuple[dict[str, list[str]], list[str]]:
    """Read an uploaded keyword file. A larger one is refused before it is parsed."""
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise KeywordFileError("The keyword file is larger than 1 MB.")
    name = Path(file.filename or "keywords.json").name or "keywords.json"
    return parse_keyword_file(data, name)
