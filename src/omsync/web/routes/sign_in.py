"""Sign-in and sign-out. The page is public; the session cookie is set only on success.

Sign-in asks the computer's owner check. omsync receives only whether it passed.
"""

import logging
import math
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from omsync.core.owner import OwnerCheckUnavailable, owner_name
from omsync.demo.site import DEMO_EMAIL
from omsync.web.auth import DEMO_SIGN_IN_PATH, SESSION_COOKIE, SIGN_IN_PATH, safe_next

router = APIRouter()
logger = logging.getLogger(__name__)
PAGE = "pages/sign_in.html"
NOT_CONFIRMED = "The computer did not confirm you. Try again."
OWNER_CHECK_REASON = "sign in to omsync"
TOO_MANY_ATTEMPTS = "Too many failed attempts. Wait a minute, then try again."


@router.get(SIGN_IN_PATH, response_class=HTMLResponse)
def sign_in_page(request: Request, next_: Annotated[str, Query(alias="next")] = "/") -> Response:
    return _render(request, next_path=safe_next(next_))


@router.post(SIGN_IN_PATH, response_class=HTMLResponse)
def sign_in(request: Request, next_: Annotated[str, Form(alias="next")] = "/") -> Response:
    state = request.app.state
    target = safe_next(next_)
    limiter = state.login_limiter

    if state.demo is not None:
        # The demo has no owner to check, so the form is never the way in.
        return _render(request, next_path=target)
    if limiter.is_blocked():
        retry = str(math.ceil(limiter.retry_after()))
        return _render(
            request,
            status_code=429,
            next_path=target,
            error=TOO_MANY_ATTEMPTS,
            headers={"Retry-After": retry},
        )
    try:
        confirmed = state.owner_check.confirm(OWNER_CHECK_REASON)
    except OwnerCheckUnavailable as exc:
        return _render(request, status_code=422, next_path=target, error=str(exc))
    if not confirmed:
        logger.warning("Sign-in refused: the owner check did not pass")
        limiter.record_failure()
        return _render(request, status_code=422, next_path=target, error=NOT_CONFIRMED)

    limiter.reset()
    return _signed_in(request, owner_name(), target)


@router.post(DEMO_SIGN_IN_PATH)
def demo_sign_in(
    request: Request, next_: Annotated[str, Form(alias="next")] = "/"
) -> RedirectResponse:
    """Enter the demo as its made-up person. The session is the same kind a sign-in makes."""
    if request.app.state.demo is None:
        raise HTTPException(status_code=404)
    return _signed_in(request, DEMO_EMAIL, safe_next(next_))


def _signed_in(request: Request, name: str, target: str) -> RedirectResponse:
    token = request.app.state.sessions.create(name)
    response = RedirectResponse(target, status_code=303)
    # No Secure flag: the server listens on 127.0.0.1 over plain HTTP.
    response.set_cookie(
        SESSION_COOKIE,
        token,
        path="/",
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/sign-out")
def sign_out(request: Request) -> RedirectResponse:
    state = request.app.state
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        state.sessions.delete(token)
    if state.vault is not None:
        state.vault.lock()
    response = RedirectResponse(SIGN_IN_PATH, status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


def _render(
    request: Request,
    *,
    status_code: int = 200,
    next_path: str,
    error: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    return request.app.state.templates.TemplateResponse(
        request,
        PAGE,
        {"next": next_path, "error": error},
        status_code=status_code,
        headers=headers,
    )
