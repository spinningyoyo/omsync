"""The home page, /, and the endpoint that saves its section layout.

The page is a shell page. Its period picker is plain links and a custom-range form, so it works
without htmx. The layout is saved by the dashboard_sortable controller with a form POST to
/home/layout, which answers 204 when it is saved and 400 with a message when it is not a layout.
The Origin check in omsync.web.auth covers this POST like every other state change.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session

from omsync.reports.dashboard import LayoutError, load_layout, parse_layout, save_layout
from omsync.web.deps import get_db
from omsync.web.home_view import build_home
from omsync.web.reports_view import DEFAULT_PRESET, select_period
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/home.html"


def _now() -> datetime:
    return datetime.now()


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    db: DbSession,
    period: str = DEFAULT_PRESET,
    start: str = "",
    end: str = "",
) -> HTMLResponse:
    now = _now()
    selection = select_period(period, start, end, now.date())
    view = build_home(db, selection, now=now, layout=load_layout(db))
    context = shell_context(request, db, active="home", title="Home")
    context["view"] = view
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.post("/home/layout")
def save_home_layout(
    db: DbSession,
    layout: Annotated[str, Form()] = "",
) -> Response:
    """Save the section order, the collapsed sections, the half-width sections, and the chosen
    heights. Unknown section keys are dropped, and a missing section is placed at its default
    position."""
    try:
        parsed = parse_layout(layout)
    except LayoutError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    save_layout(db, parsed)
    return Response(status_code=204)
