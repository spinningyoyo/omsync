"""Pages and fragments for requests that fail outside a route's own handling.

A browser asking for a page gets an HTML page with a way home, in place of FastAPI's JSON or the
bare "Internal Server Error". An htmx request gets a short message that application.js shows as a
toast: a 5xx body through htmx:responseError, or, for a 400, which htmx would swap in, the
omsync:error event with nothing swapped. Anything else keeps FastAPI's JSON.

A page load with an address omsync cannot use is answered as a missing page: an unknown path, a
wrong method, a value of the wrong type such as ?page=abc, or a number too large for SQLite's
64-bit integers. In a form, such a number is a refused value rather than a fault.
"""

import json

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException

PAGE = "pages/error.html"

NOT_FOUND = (
    "Page not found",
    "There is nothing at this address. It may have moved, or a link to it may be out of date.",
)
TOO_LARGE = "A number in this request is too large."
FAULT = (
    "Something went wrong",
    "omsync could not finish this request, and nothing was changed by it. The details are in the "
    "server's log.",
)


def install_error_pages(app: FastAPI) -> None:
    """Register the handlers. Starlette still logs the traceback of an unhandled exception."""

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        if exc.status_code in (404, 405) and _wants_page(request):
            return _page(request, *NOT_FOUND, status_code=404)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def bad_address(request: Request, exc: RequestValidationError) -> Response:
        if _wants_page(request):
            return _page(request, *NOT_FOUND, status_code=404)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(OverflowError)
    async def overflow(request: Request, exc: OverflowError) -> Response:
        if request.method == "GET":
            if _wants_page(request):
                return _page(request, *NOT_FOUND, status_code=404)
            return PlainTextResponse(NOT_FOUND[0], status_code=404)
        return PlainTextResponse(
            TOO_LARGE,
            status_code=400,
            headers={"HX-Reswap": "none", "HX-Trigger": json.dumps({"omsync:error": TOO_LARGE})},
        )

    @app.exception_handler(Exception)
    async def fault(request: Request, exc: Exception) -> Response:
        if _wants_page(request):
            return _page(request, *FAULT, status_code=500)
        return PlainTextResponse(FAULT[1], status_code=500)


def _wants_page(request: Request) -> bool:
    """A full page load in a browser, not an htmx swap or an API call."""
    if request.headers.get("HX-Request"):
        return False
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def _page(request: Request, title: str, message: str, *, status_code: int) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, PAGE, {"title": title, "message": message}, status_code=status_code
    )
