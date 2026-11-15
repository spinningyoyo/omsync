"""One receipt's page: its lines with their categories, and the transactions it matched.

The register's link menu and Reconcile open it for a vendor with no web page of its own, such as
Costco."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.receipts.triage import receipt_detail
from omsync.receipts.vendors import get_vendor
from omsync.web.deps import get_db
from omsync.web.routes.counterparty import back_href
from omsync.web.routes.shell import shell_context
from omsync.web.row_links import transaction_href

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


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
    context = shell_context(request, db, active="transactions", title="Receipt")
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
        back_href=back_href(request),
    )
    return request.app.state.templates.TemplateResponse(request, "pages/receipt.html", context)
