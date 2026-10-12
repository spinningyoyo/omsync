"""FastAPI dependencies shared by the routes."""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session


def get_db(request: Request) -> Iterator[Session]:
    """One database session per request, closed when the response is done."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
