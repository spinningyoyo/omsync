"""Sign-in and sign-out. The page is public; the session cookie is set only on success.

The password is read from the form, passed to the vault, and never stored, logged or echoed.
Failures return one generic message, so the response never says which of the two was wrong.
"""

import logging
import math
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from omsync.core.vault import VaultError
from omsync.web.auth import SESSION_COOKIE, SIGN_IN_PATH, safe_next

router = APIRouter()
logger = logging.getLogger(__name__)
PAGE = "pages/sign_in.html"
BAD_CREDENTIALS = "Incorrect email or master password"
TOO_MANY_ATTEMPTS = "Too many failed attempts. Wait a minute, then try again."


@router.get(SIGN_IN_PATH, response_class=HTMLResponse)
def sign_in_page(request: Request, next_: Annotated[str, Query(alias="next")] = "/") -> Response:
    return _render(request, next_path=safe_next(next_))


@router.post(SIGN_IN_PATH, response_class=HTMLResponse)
def sign_in(
    request: Request,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next_: Annotated[str, Form(alias="next")] = "/",
) -> Response:
    state = request.app.state
    email = email.strip()
    target = safe_next(next_)
    limiter = state.login_limiter

    if limiter.is_blocked():
        retry = str(math.ceil(limiter.retry_after()))
        return _render(
            request,
            status_code=429,
            next_path=target,
            email=email,
            error=TOO_MANY_ATTEMPTS,
            headers={"Retry-After": retry},
        )
    if not email or not password:
        return _render(
            request, status_code=422, next_path=target, email=email, error=BAD_CREDENTIALS
        )
    try:
        state.vault.unlock(email, password)
    except VaultError as exc:
        # VaultError messages are written to be safe to show and never contain the password.
        logger.warning("Sign-in refused: %s", exc)
        limiter.record_failure()
        return _render(
            request, status_code=422, next_path=target, email=email, error=BAD_CREDENTIALS
        )

    limiter.reset()
    token = state.sessions.create(email)
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
    state.vault.lock()
    response = RedirectResponse(SIGN_IN_PATH, status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


def _render(
    request: Request,
    *,
    status_code: int = 200,
    next_path: str,
    email: str = "",
    error: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    return request.app.state.templates.TemplateResponse(
        request,
        PAGE,
        {"next": next_path, "email": email, "error": error},
        status_code=status_code,
        headers=headers,
    )
