"""One receipt's page: its lines with their categories, and the transactions it matched.

The register's link menu and Reconcile open it for a vendor with no web page of its own, such as
Costco."""

from dataclasses import dataclass
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.receipts.match import MatchStatus
from omsync.receipts.model import Receipt
from omsync.receipts.service import registered_vendors, run_vendor
from omsync.receipts.triage import receipt_detail
from omsync.receipts.vendors import get_vendor
from omsync.web.deps import get_db
from omsync.web.routes.counterparty import back_href
from omsync.web.routes.shell import shell_context
from omsync.web.row_links import transaction_href

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


@dataclass(frozen=True)
class ReceiptRow:
    vendor_key: str
    vendor_label: str
    account: str
    receipt_id: str
    purchased_on: date
    total_cents: int
    is_refund: bool
    status: str  # "applied", "matched", "unmatched"
    status_label: str  # "Applied", "Matched", "Unmatched"
    item_count: int
    item_preview: str
    href: str


def _status_for(statuses: list[MatchStatus]) -> tuple[str, str]:
    if statuses and all(s is MatchStatus.already_applied for s in statuses):
        return "applied", "Applied"
    if any(s is MatchStatus.matched for s in statuses):
        return "matched", "Matched"
    return "unmatched", "Unmatched"


def _item_preview(receipt: Receipt) -> tuple[int, str]:
    item_count = len(receipt.items)
    if not item_count:
        return 0, "No items listed"
    preview = ", ".join(item.description for item in receipt.items[:2])
    if item_count > 2:
        preview += f" +{item_count - 2} more"
    return item_count, preview


def _gather_receipt_rows(db: Session, settings: Settings, today: date) -> list[ReceiptRow]:
    rows: list[ReceiptRow] = []
    seen: set[tuple[str, str]] = set()

    for vendor in registered_vendors():
        report = run_vendor(db, settings, vendor.key, today=today)
        if report.match is None:
            continue

        status_by_receipt: dict[str, list[MatchStatus]] = {}
        receipts_by_id: dict[str, Receipt] = {}

        for outcome in report.match.outcomes:
            status_by_receipt.setdefault(outcome.receipt.receipt_id, []).append(outcome.status)
            receipts_by_id[outcome.receipt.receipt_id] = outcome.receipt

        for receipt_id, receipt in receipts_by_id.items():
            key = (vendor.key, receipt_id)
            if key in seen:
                continue
            seen.add(key)

            status, status_label = _status_for(status_by_receipt.get(receipt_id, []))
            item_count, preview = _item_preview(receipt)
            total_cents = -receipt.total_cents if receipt.is_refund else receipt.total_cents

            rows.append(
                ReceiptRow(
                    vendor_key=vendor.key,
                    vendor_label=vendor.key.upper(),
                    account=receipt.account,
                    receipt_id=receipt_id,
                    purchased_on=receipt.purchased_on,
                    total_cents=total_cents,
                    is_refund=receipt.is_refund,
                    status=status,
                    status_label=status_label,
                    item_count=item_count,
                    item_preview=preview,
                    href=f"/receipts/{vendor.key}/{receipt_id}",
                )
            )
    return rows


@router.get("/receipts", response_class=HTMLResponse)
def receipts_list_page(
    request: Request,
    db: DbSession,
    q: str = "",
    vendor: str = "",
    account: str = "",
    year: str = "",
    status: str = "",
    sort: str = "date_desc",
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    today = date.today()
    all_rows = _gather_receipt_rows(db, settings, today)

    available_vendors = sorted({r.vendor_key for r in all_rows})
    available_accounts = sorted({r.account for r in all_rows})
    available_years = sorted({str(r.purchased_on.year) for r in all_rows}, reverse=True)

    rows = all_rows
    if vendor and vendor != "all":
        rows = [r for r in rows if r.vendor_key.lower() == vendor.lower()]
    if account and account != "all":
        rows = [r for r in rows if r.account.lower() == account.lower()]
    if year and year != "all":
        rows = [r for r in rows if str(r.purchased_on.year) == year]
    if status and status != "all":
        rows = [r for r in rows if r.status == status]
    if q.strip():
        query = q.strip().lower()
        rows = [
            r
            for r in rows
            if query in r.receipt_id.lower()
            or query in r.item_preview.lower()
            or query in r.account.lower()
            or query in r.vendor_key.lower()
        ]

    if sort == "date_asc":
        rows.sort(key=lambda r: (r.purchased_on, r.receipt_id))
    elif sort == "amount_desc":
        rows.sort(key=lambda r: (r.total_cents, r.purchased_on), reverse=True)
    elif sort == "amount_asc":
        rows.sort(key=lambda r: (r.total_cents, r.purchased_on))
    else:
        sort = "date_desc"
        rows.sort(key=lambda r: (r.purchased_on, r.receipt_id), reverse=True)

    context = shell_context(request, db, active="receipts", title="Receipts")
    context.update(
        rows=rows,
        total_count=len(all_rows),
        filtered_count=len(rows),
        q=q,
        vendor=vendor or "all",
        account=account or "all",
        year=year or "all",
        status=status or "all",
        sort=sort,
        available_vendors=available_vendors,
        available_accounts=available_accounts,
        available_years=available_years,
        sort_options=[
            ("date_desc", "Date: Newest first"),
            ("date_asc", "Date: Oldest first"),
            ("amount_desc", "Amount: High to low"),
            ("amount_asc", "Amount: Low to high"),
        ],
        status_options=[
            ("all", "All statuses"),
            ("applied", "Applied"),
            ("matched", "Matched"),
            ("unmatched", "Unmatched"),
        ],
    )
    return request.app.state.templates.TemplateResponse(
        request, "pages/receipts_list.html", context
    )


@router.get("/receipts/{vendor_key}/{receipt_id}", response_class=HTMLResponse)
def receipt_page(request: Request, db: DbSession, vendor_key: str, receipt_id: str) -> HTMLResponse:
    try:
        vendor = get_vendor(vendor_key)
    except KeyError:
        raise HTTPException(status_code=404, detail="No such receipt vendor") from None
    settings: Settings = request.app.state.settings
    detail = receipt_detail(db, settings, vendor_key, receipt_id, today=date.today())
    if detail is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    context = shell_context(request, db, active="receipts", title="Receipt")
    context.update(
        title=vendor.receipt_label(receipt_id),
        vendor_key=vendor_key,
        detail=detail,
        sources=[
            {
                "source": source,
                "href": transaction_href(source.account_id, source.posted_on, source.amount_cents),
            }
            for source in detail.sources
        ],
        back_href=back_href(request) or "/receipts",
    )
    return request.app.state.templates.TemplateResponse(request, "pages/receipt.html", context)
