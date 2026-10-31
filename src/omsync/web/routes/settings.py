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
GUIDES_PATH = "/settings/guides"


@router.get("/settings")
def settings_root(request: Request) -> RedirectResponse:
    # The demo has no bank sync to set up, so its Settings opens on the Guides.
    in_demo = getattr(request.app.state, "demo", None) is not None
    return RedirectResponse(GUIDES_PATH if in_demo else PROVIDERS_PATH, status_code=303)


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
        # The demo reads no Bitwarden CLI or keychain entry (docs/architecture/access.md), and its
        # data directory is a temporary one, so the checks for a real ledger do not apply.
        checks=[] if request.app.state.demo is not None else run_checks(settings),
    )
    return request.app.state.templates.TemplateResponse(request, ABOUT_PAGE, context)
