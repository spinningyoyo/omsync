"""Sign-in and sign-out routes, through FastAPI's test client with a fake owner check."""

import getpass
import tomllib
from http.cookies import Morsel, SimpleCookie
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web_support import EMAIL, PASSWORD, SESSION_COOKIE, FakeOwnerCheck, FakeVault


def _cookie(set_cookie_header: str) -> Morsel:
    jar: SimpleCookie = SimpleCookie()
    jar.load(set_cookie_header)
    return jar[SESSION_COOKIE]


def test_successful_sign_in_sets_a_strict_http_only_cookie_and_redirects(
    client: TestClient, app: FastAPI
) -> None:
    response = client.post("/sign-in", data={"next": "/reports"}, follow_redirects=False)

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
    assert session.name == getpass.getuser()


def test_a_refused_owner_check_sets_no_cookie(
    client: TestClient, owner_check: FakeOwnerCheck
) -> None:
    owner_check.passes = False

    response = client.post("/sign-in", data={"next": "/"}, follow_redirects=False)

    assert response.status_code == 422
    assert "The computer did not confirm you" in response.text
    assert client.cookies.get(SESSION_COOKIE) is None


def test_sixth_attempt_within_a_minute_is_refused_with_429(
    client: TestClient, owner_check: FakeOwnerCheck
) -> None:
    owner_check.passes = False
    for _ in range(5):
        failed = client.post("/sign-in", data={"next": "/"})
        assert failed.status_code == 422

    owner_check.passes = True
    response = client.post("/sign-in", data={"next": "/"})

    assert response.status_code == 429
    assert "Too many failed attempts" in response.text
    assert int(response.headers["retry-after"]) >= 1
    assert owner_check.calls == 5  # the refused attempt never showed the prompt


def test_forged_cookie_does_not_open_a_protected_page(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "forged-token-value")

    response = client.get("/reports", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/sign-in?next=")


def test_the_bank_sync_vault_panel_disconnects_and_connects(
    signed_in: TestClient,
    app: FastAPI,
    vault: FakeVault,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "omsync.toml"
    monkeypatch.setenv("OMSYNC_CONFIG", str(config))

    disconnected = signed_in.post("/settings/providers/vault/disconnect")
    assert disconnected.headers["hx-refresh"] == "true"
    assert app.state.vault is None
    assert vault.lock_calls == 1
    assert "No vault" in signed_in.get("/settings/providers").text

    connected = FakeVault()
    app.state.vault_opener = lambda settings: connected
    refused = signed_in.post(
        "/settings/providers/vault/connect", data={"email": EMAIL, "password": "wrong"}
    )
    assert refused.status_code == 422
    assert "Incorrect master password" in refused.text
    assert app.state.vault is None

    signed_in.post("/settings/providers/vault/connect", data={"email": EMAIL, "password": PASSWORD})
    assert app.state.vault is connected and connected.is_unlocked()
    assert tomllib.loads(config.read_text()) == {"vault": "bitwarden", "email": EMAIL}
    assert "Vault unlocked" in signed_in.get("/settings/providers").text
