"""The shell pages. Home is routes/home.py; the other rail pages are placeholders until built."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.ledger.register import open_review_count
from omsync.web.accounts_panel import account_groups, panel_grouping
from omsync.web.deps import get_db
from omsync.web.navigation import NAV_GROUPS

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def shell_context(request: Request, db: Session, *, active: str, title: str) -> dict[str, Any]:
    """The values every shell page needs: the rail, the user's email, and the accounts panel."""
    return {
        "active": active,
        "title": title,
        "nav_groups": NAV_GROUPS,
        "email": request.state.session.email,
        "account_groups": account_groups(db),
        "accounts_grouping": panel_grouping(db),
        # Counts shown on rail items. A key missing here shows no badge.
        "badges": {"transactions": open_review_count(db)},
    }


def _render(
    request: Request, db: Session, template: str, *, active: str, title: str
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, template, shell_context(request, db, active=active, title=title)
    )
