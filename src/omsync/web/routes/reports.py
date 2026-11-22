"""The reports page: /reports, the Export dialog's routes, and the print layout.

The page is a shell page. Its period choices are plain links, and the joint choice is the shared
one in routes/shell.py, which reloads the page. The Export dialog's year select asks for its
workbook choices with htmx (reports/fragments.html).

The Export dialog downloads the two yearly workbooks from omsync.reports.exports. Each download is
built in a temporary file in the exports directory, moved to its default name there, and sent as an
attachment. The file stays in the exports directory, as the CLI's default path does.
"""

import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy.orm import Session

from omsync.ledger.rental import get_rental_accounts
from omsync.reports import exports
from omsync.web.deps import get_db
from omsync.web.formats import long_day
from omsync.web.reports_view import (
    DEFAULT_PRESET,
    Selection,
    build_report,
    select_period,
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
BAD_REPORT = "Choose the transactions workbook or the rental report."
EXPORT_REPORTS = ("transactions", "rental")
WRITE_FAILED = "The workbook could not be written to the exports folder."


@dataclass(frozen=True)
class ExportMenu:
    """What the Export dialog shows: the year its choices use, the years the year select offers, and
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
    tag: Annotated[list[int] | None, Query()] = None,
) -> HTMLResponse:
    today = date.today()
    selection = _with_tags(select_period(period, start, end, today), tag)
    view = build_report(db, selection, today=today)
    context = shell_context(request, db, active="reports", title="Reports")
    context["view"] = view
    context["export"] = _export_menu(db, selection, today=today)
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.get("/reports/export/menu", response_class=HTMLResponse)
def export_menu(request: Request, db: DbSession, year: str = "") -> Response:
    """The workbook choices for the year the select picked. htmx swaps them in for the old ones."""
    chosen = _requested_year(year, date.today())
    if chosen is None:
        return PlainTextResponse(BAD_YEAR, status_code=400)
    menu = ExportMenu(year=chosen, years=[], rental_ready=bool(get_rental_accounts(db)))
    return request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, {"export": menu}, block_name="export_menu_reply"
    )


@router.get("/reports/export")
def export_chosen(year: str = "", report: str = "transactions") -> Response:
    """The Export dialog's form: send it on to the chosen workbook's download for the year. The
    download checks the year and, for the rental report, that properties are set."""
    if report not in EXPORT_REPORTS:
        return PlainTextResponse(BAD_REPORT, status_code=400)
    query = urlencode({"year": year})
    return RedirectResponse(f"/reports/export/{report}?{query}", status_code=303)


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


@router.get("/reports/print", response_class=HTMLResponse)
def reports_print(
    request: Request,
    db: DbSession,
    period: str = DEFAULT_PRESET,
    start: str = "",
    end: str = "",
    tag: Annotated[list[int] | None, Query()] = None,
) -> HTMLResponse:
    """The report laid out for paper: no shell, and the print styles from print-report.css."""
    today = date.today()
    selection = _with_tags(select_period(period, start, end, today), tag)
    view = build_report(db, selection, today=today)
    return request.app.state.templates.TemplateResponse(
        request,
        PRINT,
        {"view": view, "printed": long_day(today)},
    )


def _with_tags(selection: Selection, tag_ids: list[int] | None) -> Selection:
    """The selection limited to the tags in the query, each once."""
    return replace(selection, tag_ids=tuple(dict.fromkeys(tag_ids or ())))


def _export_menu(db: Session, selection: Selection, *, today: date) -> ExportMenu:
    """The Export dialog for a period: its year is the year the period ends in, when that year is
    one the exports accept, otherwise this year."""
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
        temp.replace(target)
    except exports.NoRentalAccounts:
        return PlainTextResponse(NO_RENTAL, status_code=409)
    except OSError:
        return PlainTextResponse(WRITE_FAILED, status_code=500)
    finally:
        temp.unlink(missing_ok=True)
    return FileResponse(target, media_type=XLSX, filename=name)
