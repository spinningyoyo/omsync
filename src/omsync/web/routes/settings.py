"""The Settings root and the About page. Bank sync is in settings_providers.py."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from omsync import __version__
from omsync.core.doctor import run_checks
from omsync.web.deps import get_db
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
ABOUT_PAGE = "pages/settings_about.html"
PROVIDERS_PATH = "/settings/providers"


@router.get("/settings")
def settings_root() -> RedirectResponse:
    return RedirectResponse(PROVIDERS_PATH, status_code=303)


@router.get("/settings/about", response_class=HTMLResponse)
def about_page(request: Request, db: DbSession) -> HTMLResponse:
    settings = request.app.state.settings
    context = shell_context(request, db, active="", title="About")
    context.update(
        nav=SETTINGS_NAV,
        active_href="/settings/about",
        page_title="About",
        version=__version__,
        data_dir=str(settings.data_dir),
        checks=run_checks(settings),
    )
    return request.app.state.templates.TemplateResponse(request, ABOUT_PAGE, context)
