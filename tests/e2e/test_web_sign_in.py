"""Sign-in and sign-out routes, through FastAPI's test client with a fake vault."""

from http.cookies import Morsel, SimpleCookie

from fastapi import FastAPI
from fastapi.testclient import TestClient
from web_support import (
    EMAIL,
    PASSWORD,
    SESSION_COOKIE,
    FakeVault,
)

WRONG_PASSWORD = "wrong-guess-7f3a"


def _cookie(set_cookie_header: str) -> Morsel:
    jar: SimpleCookie = SimpleCookie()
    jar.load(set_cookie_header)
    return jar[SESSION_COOKIE]


def test_successful_sign_in_sets_a_strict_http_only_cookie_and_redirects(
    client: TestClient, app: FastAPI
) -> None:
    response = client.post(
        "/sign-in",
        data={"email": EMAIL, "password": PASSWORD, "next": "/reports"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/reports"
    cookie = _cookie(response.headers["set-cookie"])
    assert cookie["httponly"]
    assert cookie["samesite"].lower() == "strict"
    assert cookie["path"] == "/"
    assert not cookie["secure"]  # plain HTTP on 127.0.0.1
    token = client.cookies.get(SESSION_COOKIE)
    assert token is not None
    assert len(token) == 43
    session = app.state.sessions.get(token)
    assert session is not None
    assert session.email == EMAIL


def test_password_never_appears_in_any_sign_in_response(client: TestClient) -> None:
    responses = [
        client.post("/sign-in", data={"email": EMAIL, "password": WRONG_PASSWORD}),
        client.post("/sign-in", data={"email": EMAIL, "password": PASSWORD}),
        client.get("/sign-in", params={"next": "/"}),
    ]

    for response in responses:
        assert PASSWORD not in response.text
        assert WRONG_PASSWORD not in response.text


def test_sixth_attempt_within_a_minute_is_refused_with_429(
    client: TestClient, vault: FakeVault
) -> None:
    for _ in range(5):
        failed = client.post("/sign-in", data={"email": EMAIL, "password": WRONG_PASSWORD})
        assert failed.status_code == 422

    response = client.post("/sign-in", data={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 429
    assert "Too many failed attempts" in response.text
    assert int(response.headers["retry-after"]) >= 1
    assert len(vault.unlock_calls) == 5  # the refused attempt never reached the vault


def test_forged_cookie_does_not_open_a_protected_page(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "forged-token-value")

    response = client.get("/reports", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/sign-in?next=")
