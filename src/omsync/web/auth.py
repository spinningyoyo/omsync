"""The session guard: every route needs a live app session except the public ones.

Installed as an HTTP middleware, so a route added later is protected by default. It also refuses
state-changing requests whose Origin header does not match the host, which blocks cross-site
form posts. The session cookie is SameSite=Strict, so a cross-site request never carries it.
"""

from collections.abc import Awaitable, Callable
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response

SESSION_COOKIE = "omsync_session"
SIGN_IN_PATH = "/sign-in"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_PATHS = frozenset({SIGN_IN_PATH, "/health"})
# /dev and /prototype serve fake data only, so they need no session.
PUBLIC_PREFIXES = ("/static/", "/dev/", "/prototype/")
_MAX_NEXT_LENGTH = 2048


def safe_next(value: str | None) -> str:
    """A same-site path to return to after sign-in. Anything else becomes "/".

    Only paths that start with a single "/" are accepted, so "//host", "/\\host", absolute URLs,
    control characters and non-ASCII characters all fall back to "/". Returning to the sign-in
    page itself also falls back to "/".
    """
    if not value or len(value) > _MAX_NEXT_LENGTH:
        return "/"
    if not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "/"
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path == SIGN_IN_PATH:
        return "/"
    return value


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def same_origin(request: Request) -> bool:
    """True when no Origin header is sent, or when it names this request's scheme and host."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host", "")
    parts = urlsplit(origin)
    return parts.scheme == request.url.scheme and parts.netloc.lower() == host.lower()


def install_guard(app: FastAPI) -> None:
    """Register the middleware. It reads the session store from app.state at request time."""

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if request.method in UNSAFE_METHODS and not same_origin(request):
            return _no_store(Response("Cross-site request refused.", status_code=403), path)
        if not is_public(path):
            token = request.cookies.get(SESSION_COOKIE)
            session = request.app.state.sessions.get(token) if token else None
            if session is None:
                return _no_store(_unauthenticated(request), path)
            request.state.session = session
        return _no_store(await call_next(request), path)


def _unauthenticated(request: Request) -> Response:
    if request.headers.get("HX-Request"):
        # htmx follows HX-Redirect on any status, so the swap never happens.
        return Response(status_code=401, headers={"HX-Redirect": SIGN_IN_PATH})
    if request.method == "GET":
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"{SIGN_IN_PATH}?next={quote(target, safe='')}", status_code=303)
    return RedirectResponse(SIGN_IN_PATH, status_code=303)


def _no_store(response: Response, path: str) -> Response:
    """Keep pages out of the browser's cache, so Back after sign-out re-checks the session.

    Static files may be cached, so they are left alone.
    """
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response
