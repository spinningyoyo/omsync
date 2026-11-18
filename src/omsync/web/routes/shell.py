"""The shell pages. Home is routes/home.py; the other rail pages are placeholders until built."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from omsync.ledger.preferences import JOINT_SHARE, set_pref
from omsync.ledger.register import open_review_count
from omsync.web.accounts_panel import account_groups, panel_grouping
from omsync.web.deps import get_db
from omsync.web.joint_view import JOINT_FULL, JOINT_HALF, joint_view
from omsync.web.navigation import NAV_GROUPS
from omsync.web.vault_logins import vault_status_view

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def shell_context(request: Request, db: Session, *, active: str, title: str) -> dict[str, Any]:
    """The values every shell page needs: the rail, who is signed in and the vault's status for the
    user menu, and the accounts panel."""
    return {
        "active": active,
        "title": title,
        "nav_groups": NAV_GROUPS,
        "user_name": request.state.session.name,
        "vault_status": vault_status_view(request.app.state.vault),
        "account_groups": account_groups(db),
        "accounts_grouping": panel_grouping(db),
        # Counts shown on rail items. A key missing here shows no badge.
        "badges": {"transactions": open_review_count(db)},
        # None when there is no joint account, so the joint control is not shown.
        "joint": joint_view(db),
    }


@router.post("/preferences/joint-share")
def set_joint_share(
    db: DbSession,
    joint: Annotated[str, Form()] = JOINT_HALF,
    return_to: Annotated[str, Form()] = "/",
) -> RedirectResponse:
    """Save the joint-share choice, then reload the page it was made on, since every amount on it
    may change. return_to must be a path on this site; anything else goes home."""
    set_pref(db, JOINT_SHARE, joint != JOINT_FULL)
    safe = return_to.startswith("/") and not return_to.startswith("//") and "\\" not in return_to
    return RedirectResponse(return_to if safe else "/", status_code=303)


def _render(
    request: Request, db: Session, template: str, *, active: str, title: str
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, template, shell_context(request, db, active=active, title=title)
    )
