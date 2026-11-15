"""Triage and keyword import: the ways unclassified payees and receipt items become keywords and
then categories.

A keyword added here, or imported, applies at once: the automatic step follows the write in the
same request, and undo reverses both (see applied_toast). A receipt keyword also applies the
vendor's receipts, so the charges it completes are split at once, and undo reverses that too.
Triage previews a keyword before it is added, as the Keywords tab does. Import previews the file
first, and its apply is refused when the data moved on since that preview.
"""

import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError, applied_batch_for_key
from omsync.core.settings import Settings
from omsync.ledger import categories_admin as admin
from omsync.ledger.bulk import count_matching
from omsync.ledger.categorize import PAYEE_LIST, list_name
from omsync.ledger.keyword_import import (
    KeywordFileError,
    apply_keyword_import,
    parse_keyword_file,
    parse_keyword_mapping,
    plan_keyword_import,
)
from omsync.ledger.receipt_vendors import RECEIPT_VENDOR_KEYS
from omsync.ledger.register import keep_category
from omsync.receipts import triage as receipt_triage
from omsync.receipts.service import apply_after_keyword
from omsync.reports.attention import UNCATEGORIZED
from omsync.web.categories_replies import (
    applied_toast,
    block,
    events,
    refused,
    toast,
    whole_number,
)
from omsync.web.deps import get_db
from omsync.web.register_state import query_string
from omsync.web.routes.categories_keywords import keyword_form, safe_list
from omsync.web.row_links import transaction_href

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
STALE = "This changed elsewhere. Preview again."
OUT_OF_DATE = "The import is out of date. Choose the file again."
MAX_UPLOAD_BYTES = 1024 * 1024
_SLOT = re.compile(r"[a-z0-9_-]{1,60}")
UNCATEGORIZED_HREF = "/transactions?" + query_string(None, UNCATEGORIZED, None)
PAYEES = "payees"


# Triage


@router.get("/categories/triage", response_class=HTMLResponse)
def triage(request: Request, db: DbSession, source: str = PAYEES) -> HTMLResponse:
    return block(request, "frag_triage", **triage_context(request, db, source))


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
    target_list = PAYEE_LIST if kind == "payee" else safe_list(list_key)
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
    list_key: Annotated[str, Form()] = PAYEE_LIST,
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
    reply = applied_toast(db, batch)
    if kind == "receipt" and list_key in RECEIPT_VENDOR_KEYS:
        reply["label"] = _with_receipts(request, db, list_key, batch.id, reply["label"])
    return block(
        request,
        "frag_triage_done",
        trigger=events(changed=True),
        slot=slot,
        list_key=list_key,
        **reply,
    )


@router.post("/categories/triage/keep", response_class=HTMLResponse)
def triage_keep_category(
    request: Request,
    db: DbSession,
    transaction_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Keep a receipt's transaction in the category it has, so its receipt no longer waits."""
    try:
        batch = keep_category(
            db,
            whole_number(transaction_id, "Choose a transaction."),
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Transaction not found") from None
    except (ValueError, BatchError) as exc:
        return block(request, "frag_toast", status=refused(exc), message=str(exc))
    return block(request, "frag_toast", trigger=events(changed=True), **toast(batch))


# Keyword import


@router.post("/categories/import/preview", response_class=HTMLResponse)
def import_preview(
    request: Request,
    db: DbSession,
    file: Annotated[UploadFile, File()],
    list_key: Annotated[str, Form()] = PAYEE_LIST,
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
    list_key: Annotated[str, Form()] = PAYEE_LIST,
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
    message = (
        f"Imported {count} {'keyword' if count == 1 else 'keywords'} to {list_name(list_key)}."
    )
    return block(
        request,
        "frag_import_done",
        trigger=events(changed=True, close=True),
        message=message,
        **applied_toast(db, batch, label=message),
    )


# Helpers


def triage_context(request: Request, db: Session, source: str = PAYEES) -> dict[str, Any]:
    """Payees and each receipt vendor, as one tab each. A vendor with nothing waiting has no tab,
    unless it is the tab open now, so adding its last keyword does not take the tab away."""
    settings: Settings = request.app.state.settings
    today = date.today()
    payees = admin.uncategorized_payees(db)
    vendors = []
    for key in RECEIPT_VENDOR_KEYS:
        items = receipt_triage.waiting_items(db, settings, key, today=today)
        if items or key == source:
            vendors.append(
                {
                    "key": key,
                    "label": key.capitalize(),
                    "items": [_item_view(item) for item in items],
                    "has_files": receipt_triage.has_receipt_files(settings, key),
                }
            )
    shown = {PAYEES, *(vendor["key"] for vendor in vendors)}
    return {
        # The count Home shows, over the filter its link and this one open in Transactions.
        "uncategorized": count_matching(db, UNCATEGORIZED),
        "uncategorized_href": UNCATEGORIZED_HREF,
        "payees": payees,
        "vendors": vendors,
        "source": source if source in shown else PAYEES,
        "choices": admin.category_choices(db),
    }


def _item_view(item: receipt_triage.WaitingItem) -> dict[str, Any]:
    return {
        "text": item.text,
        "sources": [
            {
                "source": source,
                "href": transaction_href(source.account_id, source.posted_on, source.amount_cents),
            }
            for source in item.sources
        ],
    }


def _with_receipts(
    request: Request, db: Session, vendor_key: str, batch_id: int, label: str
) -> str:
    """Apply the vendor's receipts after its keyword, and say what that did."""
    settings: Settings = request.app.state.settings
    try:
        applied = apply_after_keyword(
            db, settings, vendor_key, today=date.today(), triggered_by=batch_id
        )
    except BatchError as exc:
        db.rollback()
        return f"{label.rstrip('.')}. Receipts could not apply: {exc}"
    if applied is None:
        return label
    count = int((applied.summary or {}).get("applied", 0))
    noun = "receipt charge" if count == 1 else "receipt charges"
    return f"{label.rstrip('.')}. Applied {count} {noun}."


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
