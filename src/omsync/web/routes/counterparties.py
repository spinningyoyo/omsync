"""Counterparty settings and pattern-test responses."""

import json
import re
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError
from omsync.core.money import format_cents
from omsync.ledger.counterparties import (
    CounterpartyMatcher,
    NothingChanged,
    create_counterparty,
    delete_counterparty,
    list_counterparties,
    load_matchers,
    update_counterparty,
)
from omsync.ledger.links import CounterpartyKind
from omsync.ledger.sources import Account, account_digits
from omsync.ledger.transactions import Transaction
from omsync.web.deps import get_db
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/settings_counterparties.html"
FRAGMENTS = "settings/counterparty_fragments.html"
PATH = "/settings/counterparties"
TEST_LIMIT = 20
KIND_LABELS = {
    CounterpartyKind.person.value: "Person",
    CounterpartyKind.business.value: "Business",
}


@dataclass(frozen=True)
class CounterpartyRow:
    id: int
    name: str
    kind: str
    kind_label: str
    out: tuple[str, ...]
    inbound: tuple[str, ...]
    out_text: str
    in_text: str
    no_match_expected: bool
    edit_key: str
    delete_key: str


@dataclass(frozen=True)
class TestRow:
    on: str
    account: str
    payee: str
    amount: str


# Pages


@router.get("/settings/counterparties", response_class=HTMLResponse)
def counterparties_page(request: Request, db: DbSession) -> HTMLResponse:
    context = _page_context(request, db)
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.get("/settings/counterparties/test", response_class=HTMLResponse)
def test_box(request: Request, db: DbSession, counterparty: int | None = None) -> HTMLResponse:
    """The matches for one counterparty: up to 20 recent money-out and money-in lines."""
    matcher = _matcher(db, counterparty) if counterparty is not None else None
    if matcher is None:
        return _fragment(request, "frag_test_results", matcher=None, out_rows=[], in_rows=[])
    return _fragment(
        request,
        "frag_test_results",
        matcher=matcher,
        out_rows=_recent(db, matcher.out_patterns, outflow=True),
        in_rows=_recent(db, matcher.in_patterns, outflow=False),
    )


# Writes


@router.post("/settings/counterparties", response_class=HTMLResponse)
def create_route(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = CounterpartyKind.business.value,
    out: Annotated[str, Form()] = "",
    inbound: Annotated[str, Form(alias="in")] = "",
    no_match_expected: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        batch = create_counterparty(
            db,
            name=name,
            kind=kind,
            payee_patterns={"out": _lines(out), "in": _lines(inbound)},
            no_match_expected=bool(no_match_expected),
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except (ValueError, BatchError) as exc:
        return _fragment(request, "frag_form_error", status=422, message=str(exc))
    return _saved(request, db, batch)


@router.post("/settings/counterparties/{counterparty_id}", response_class=HTMLResponse)
def update_route(
    request: Request,
    db: DbSession,
    counterparty_id: int,
    name: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = CounterpartyKind.business.value,
    out: Annotated[str, Form()] = "",
    inbound: Annotated[str, Form(alias="in")] = "",
    no_match_expected: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        batch = update_counterparty(
            db,
            counterparty_id,
            name=name,
            kind=kind,
            payee_patterns={"out": _lines(out), "in": _lines(inbound)},
            no_match_expected=bool(no_match_expected),
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such counterparty") from None
    except NothingChanged as exc:
        return _fragment(request, "frag_form_error", status=200, message=str(exc))
    except (ValueError, BatchError) as exc:
        return _fragment(request, "frag_form_error", status=422, message=str(exc))
    return _saved(request, db, batch)


@router.post("/settings/counterparties/{counterparty_id}/delete", response_class=HTMLResponse)
def delete_route(
    request: Request,
    db: DbSession,
    counterparty_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        batch = delete_counterparty(
            db, counterparty_id, idempotency_key=idempotency_key or uuid4().hex
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such counterparty") from None
    return _saved(request, db, batch)


def counterparty_undo_reply(request: Request, db: Session, message: str) -> HTMLResponse:
    """After an undo of a counterparty batch: the toast, and the list re-read out of band."""
    return _fragment(
        request,
        "frag_undo_reply",
        message=message,
        rows=_rows(db),
        create_key=uuid4().hex,
    )


# Helpers


def _saved(request: Request, db: Session, batch: Batch) -> HTMLResponse:
    summary = batch.summary or {}
    return _fragment(
        request,
        "frag_saved",
        trigger={"close-dialogs": True},
        rows=_rows(db),
        create_key=uuid4().hex,
        message=str(summary.get("label", "Change saved")),
        batch_id=batch.id,
    )


def _page_context(request: Request, db: Session) -> dict[str, Any]:
    context = shell_context(request, db, active="", title="Counterparties")
    context.update(
        nav=SETTINGS_NAV,
        active_href=PATH,
        page_title="Counterparties",
        rows=_rows(db),
        create_key=uuid4().hex,
    )
    return context


def _rows(db: Session) -> list[CounterpartyRow]:
    rows: list[CounterpartyRow] = []
    for row in list_counterparties(db):
        out = tuple(row.payee_patterns["out"])
        inbound = tuple(row.payee_patterns["in"])
        rows.append(
            CounterpartyRow(
                id=row.id,
                name=row.name,
                kind=row.kind.value,
                kind_label=KIND_LABELS.get(row.kind.value, row.kind.value),
                out=out,
                inbound=inbound,
                out_text="\n".join(out),
                in_text="\n".join(inbound),
                no_match_expected=row.no_match_expected,
                edit_key=uuid4().hex,
                delete_key=uuid4().hex,
            )
        )
    return rows


def _matcher(db: Session, counterparty_id: int) -> CounterpartyMatcher | None:
    for matcher in load_matchers(db):
        if matcher.id == counterparty_id:
            return matcher
    return None


def _recent(db: Session, patterns: tuple[re.Pattern[str], ...], *, outflow: bool) -> list[TestRow]:
    """Up to 20 of the newest transactions whose raw payee matches a pattern, on one side."""
    stmt = (
        select(Transaction, Account)
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.amount_cents < 0 if outflow else Transaction.amount_cents > 0)
        .order_by(Transaction.posted_on.desc(), Transaction.id.desc())
    )
    found: list[TestRow] = []
    if not patterns:
        return found
    result = db.execute(stmt.execution_options(yield_per=500))
    try:
        for txn, account in result:
            if any(pattern.search(txn.payee_raw) for pattern in patterns):
                found.append(
                    TestRow(
                        on=txn.posted_on.isoformat(),
                        account=account_digits(account.last4, account.alias_last4) or "—",
                        payee=txn.payee_raw,
                        amount=format_cents(txn.amount_cents),
                    )
                )
                if len(found) == TEST_LIMIT:
                    break
    finally:
        result.close()
    return found


def _lines(text: str) -> list[str]:
    """One pattern per line. Blank lines are dropped; the ledger module checks each pattern."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _fragment(
    request: Request,
    block: str,
    *,
    status: int = 200,
    trigger: dict[str, bool] | None = None,
    **context: Any,
) -> HTMLResponse:
    response = request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, context, status_code=status, block_name=block
    )
    if trigger:
        response.headers["HX-Trigger"] = json.dumps(trigger)
    return response
