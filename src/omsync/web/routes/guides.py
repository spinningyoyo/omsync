"""The Guides in Settings: the index and one page per guide. The text is in templates/guides; the
registry of guides is in web/guides.py."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.web.deps import get_db
from omsync.web.guides import figure_files, grouped_guides, guide_for, neighbours
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
GUIDES_PATH = "/settings/guides"
INDEX_TEMPLATE = "guides/index.html"


def _guide_context(request: Request, db: Session, title: str) -> dict[str, Any]:
    context = shell_context(request, db, active="", title=title)
    context.update(nav=SETTINGS_NAV, active_href=GUIDES_PATH)
    return context


@router.get(GUIDES_PATH, response_class=HTMLResponse)
def guides_index(request: Request, db: DbSession) -> HTMLResponse:
    context = _guide_context(request, db, title="Guides")
    context.update(groups=grouped_guides())
    return request.app.state.templates.TemplateResponse(request, INDEX_TEMPLATE, context)


@router.get(f"{GUIDES_PATH}/{{slug}}", response_class=HTMLResponse)
def guide_page(slug: str, request: Request, db: DbSession) -> HTMLResponse:
    guide = guide_for(slug)
    if guide is None:
        raise HTTPException(status_code=404, detail="No such guide")
    previous, following = neighbours(slug)
    context = _guide_context(request, db, title=guide.title)
    context.update(
        guide=guide,
        previous_guide=previous,
        next_guide=following,
        figures=figure_files(),
    )
    return request.app.state.templates.TemplateResponse(
        request, f"guides/{guide.slug}.html", context
    )
