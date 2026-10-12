"""The reports page: /reports, its period picker, the joint share toggle, the Export menu, and the
print layout.

The page is a shell page. A period change or a toggle is an htmx request that returns out-of-band
swaps (reports/fragments.html), so the controls keep focus where the swap leaves them. Without
htmx, the toggle redirects back to the page and the period links are ordinary links.

The Export menu downloads the two yearly workbooks from omsync.reports.exports. Each download is
built in a temporary file in the exports directory, moved to its default name there, and sent as an
attachment. The file stays in the exports directory, as the CLI's default path does.
"""

import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy.orm import Session

from omsync.ledger.preferences import JOINT_SHARE, set_pref
from omsync.reports import exports
from omsync.reports.rental import get_rental_accounts
from omsync.web.deps import get_db
from omsync.web.reports_view import (
    DEFAULT_PRESET,
    ReportView,
    Selection,
    build_report,
    select_period,
    state_query,
)
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/reports.html"
PRINT = "pages/reports_print.html"
FRAGMENTS = "reports/fragments.html"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FIRST_EXPORT_YEAR = 2000
YEARS_OFFERED = 5
BAD_YEAR = "Choose a year from 2000 to next year, as four digits."
NO_RENTAL = "Set up rental properties in Settings before you export the rental report."
WRITE_FAILED = "The workbook could not be written to the exports folder."


@dataclass(frozen=True)
class ExportMenu:
    """What the Export menu shows: the year its items use, the years the year select offers, and
    whether rental properties are set, which enables the rental item."""

    year: int
    years: list[int]
    rental_ready: bool


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    db: DbSession,
    period: str = DEFAULT_PRESET,
    start: str = "",
    end: str = "",
) -> HTMLResponse:
    today = date.today()
    selection = select_period(period, start, end, today)
    if _is_htmx(request):
        return _reply(request, db, selection, with_export=True)
    view = build_report(db, selection, today=today)
    context = shell_context(request, db, active="reports", title="Reports")
    context["view"] = view
    context["export"] = _export_menu(db, selection, today=today)
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.get("/reports/export/menu", response_class=HTMLResponse)
def export_menu(request: Request, db: DbSession, year: str = "") -> Response:
    """The Export menu for the year the select picked. htmx swaps it in place of the old menu."""
    chosen = _requested_year(year, date.today())
    if chosen is None:
        return PlainTextResponse(BAD_YEAR, status_code=400)
    menu = ExportMenu(year=chosen, years=[], rental_ready=bool(get_rental_accounts(db)))
    return request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, {"export": menu}, block_name="export_menu_reply"
    )


@router.get("/reports/export/transactions")
def export_transactions_workbook(request: Request, db: DbSession, year: str = "") -> Response:
    """The year's transactions workbook, as an attachment named transactions_YYYY.xlsx."""
    chosen = _requested_year(year, date.today())
    if chosen is None:
        return PlainTextResponse(BAD_YEAR, status_code=400)
    exports_dir = request.app.state.settings.exports_dir
    return _send_workbook(
        request,
        f"transactions_{chosen}.xlsx",
        lambda temp: exports.export_transactions(db, chosen, temp, exports_dir=exports_dir),
    )


@router.get("/reports/export/rental")
def export_rental_workbook(request: Request, db: DbSession, year: str = "") -> Response:
    """The year's rental workbook, as an attachment named rental_YYYY.xlsx. It needs at least one
    rental property, or the answer is 409 with the way to set one up."""
    chosen = _requested_year(year, date.today())
    if chosen is None:
        return PlainTextResponse(BAD_YEAR, status_code=400)
    if not get_rental_accounts(db):
        return PlainTextResponse(NO_RENTAL, status_code=409)
    exports_dir = request.app.state.settings.exports_dir
    return _send_workbook(
        request,
        f"rental_{chosen}.xlsx",
        lambda temp: exports.export_rental(db, chosen, temp, exports_dir=exports_dir),
    )


@router.post("/reports/joint-share", response_class=HTMLResponse)
def set_joint_share(
    request: Request,
    db: DbSession,
    joint: Annotated[str, Form()] = "",
    period: Annotated[str, Form()] = DEFAULT_PRESET,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
) -> Response:
    """Save the toggle. The checkbox posts "1" when ticked and nothing when cleared, so an absent
    box means joint share is off."""
    set_pref(db, JOINT_SHARE, joint == "1")
    selection = select_period(period, start, end, date.today())
    if _is_htmx(request):
        # The year select keeps the year the user picked, so the toggle does not re-render it.
        return _reply(request, db, selection, with_export=False)
    return RedirectResponse(f"/reports?{state_query(selection)}", status_code=303)


@router.get("/reports/print", response_class=HTMLResponse)
def reports_print(
    request: Request,
    db: DbSession,
    period: str = DEFAULT_PRESET,
    start: str = "",
    end: str = "",
) -> HTMLResponse:
    """The report laid out for paper: no shell, and the print styles from print-report.css."""
    today = date.today()
    view = build_report(db, select_period(period, start, end, today), today=today)
    return request.app.state.templates.TemplateResponse(
        request,
        PRINT,
        {"view": view, "printed": today.strftime("%b %-d, %Y")},
    )


def _reply(
    request: Request, db: Session, selection: Selection, *, with_export: bool
) -> HTMLResponse:
    """The out-of-band swaps for an htmx request: the controls, the hidden state, the report, and,
    for a period change, the Export menu for the new period's year."""
    today = date.today()
    view: ReportView = build_report(db, selection, today=today)
    context: dict[str, object] = {"view": view}
    if with_export:
        context["export"] = _export_menu(db, selection, today=today)
    return request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, context, block_name="reply"
    )


def _export_menu(db: Session, selection: Selection, *, today: date) -> ExportMenu:
    """The Export menu for a period: its year is the year the period ends in, when that year is one
    the exports accept, otherwise this year."""
    end_year = selection.period.end.year
    year = end_year if _year_in_range(end_year, today) else today.year
    offered = set(range(today.year - YEARS_OFFERED + 1, today.year + 1))
    offered.add(year)
    return ExportMenu(
        year=year,
        years=sorted(offered, reverse=True),
        rental_ready=bool(get_rental_accounts(db)),
    )


def _requested_year(raw: str, today: date) -> int | None:
    """The year a request asks for: exactly four digits, from 2000 to next year. None otherwise."""
    if not re.fullmatch(r"[0-9]{4}", raw):
        return None
    year = int(raw)
    return year if _year_in_range(year, today) else None


def _year_in_range(year: int, today: date) -> bool:
    return FIRST_EXPORT_YEAR <= year <= today.year + 1


def _send_workbook(request: Request, name: str, build: Callable[[Path], Path]) -> Response:
    """Build one workbook into a temporary file in the exports directory, move it to its default
    name there, and send it as an attachment. The move is atomic, so a half-written file is never
    served. A workbook that cannot be built is a 409 (no rental properties) or a 500 (write failed).
    """
    exports_dir = request.app.state.settings.exports_dir
    try:
        exports_dir.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=exports_dir, prefix=".", suffix=".xlsx.part")
        os.close(handle)
    except OSError:
        return PlainTextResponse(WRITE_FAILED, status_code=500)
    temp = Path(temp_name)
    target = exports_dir / name
    try:
        build(temp)
        os.replace(temp, target)
    except exports.NoRentalAccounts:
        return PlainTextResponse(NO_RENTAL, status_code=409)
    except OSError:
        return PlainTextResponse(WRITE_FAILED, status_code=500)
    finally:
        temp.unlink(missing_ok=True)
    return FileResponse(target, media_type=XLSX, filename=name)


def _is_htmx(request: Request) -> bool:
    """A request htmx made for a swap. A history restore wants the whole page instead."""
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )
