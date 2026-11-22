"""Sign-in and sign-out routes, through FastAPI's test client with a fake owner check."""

import getpass
import tomllib
from datetime import UTC, datetime
from http.cookies import Morsel, SimpleCookie
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from web_support import EMAIL, PASSWORD, SESSION_COOKIE, FakeOwnerCheck, FakeVault

from omsync.ledger.models import Provider, SyncRun, SyncStatus
from omsync.receipts.profiles import VendorProfile, create_vendor_profile


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

    disconnected = signed_in.post("/settings/sync/vault/disconnect")
    assert disconnected.headers["hx-refresh"] == "true"
    assert app.state.vault is None
    assert vault.lock_calls == 1
    assert "No vault" in signed_in.get("/settings/sync").text

    connected = FakeVault()
    app.state.vault_opener = lambda settings: connected
    refused = signed_in.post(
        "/settings/sync/vault/connect", data={"email": EMAIL, "password": "wrong"}
    )
    assert refused.status_code == 422
    assert "Incorrect master password" in refused.text
    assert app.state.vault is None

    signed_in.post("/settings/sync/vault/connect", data={"email": EMAIL, "password": PASSWORD})
    assert app.state.vault is connected and connected.is_unlocked()
    assert tomllib.loads(config.read_text()) == {"vault": "bitwarden", "email": EMAIL}
    assert "Vault unlocked" in signed_in.get("/settings/sync").text

    with app.state.session_factory() as db:
        create_vendor_profile(
            db,
            VendorProfile(
                key="amazon_personal",
                vendor_key="amazon",
                name="Amazon Personal",
                vault_item_id="item-amazon",
            ),
            idempotency_key="test-amazon-personal",
        )
        db.add(
            SyncRun(
                provider_id=db.scalars(
                    select(Provider.id).where(Provider.key == "amazon_personal")
                ).one(),
                started_at=datetime(2026, 11, 18, 12, 7, 0, tzinfo=UTC).replace(tzinfo=None),
                finished_at=datetime(2026, 11, 18, 12, 8, 0, tzinfo=UTC).replace(tzinfo=None),
                status=SyncStatus.succeeded,
                items_fetched=14,
            )
        )
        db.commit()

    sync_page = signed_in.get("/settings/sync?tab=receipts")
    assert sync_page.status_code == 200
    assert "Last sync" in sync_page.text
    assert "Succeeded" in sync_page.text
    assert "14 receipts" in sync_page.text
    assert "Amazon Personal" in sync_page.text
    assert ">amazon_personal<" not in sync_page.text

    assert signed_in.get("/settings/sync/amazon_personal/edit/login").text == "Amazon (Ana)"

    locked = signed_in.post("/settings/sync/vault/lock")
    assert locked.status_code == 200 and "Unlock" in locked.text
    assert not connected.is_unlocked()
    assert "Picked" in signed_in.get("/settings/sync?tab=receipts").text
    assert signed_in.get("/settings/sync/amazon_personal/edit/login").text == "Picked"

    search = signed_in.get("/settings/sync/amazon_personal/edit/logins?q=amazon")
    assert "hx-trigger" not in search.headers
    assert 'name="place" value="inline"' in search.text
    assert 'id="provider-edit-form-amazon_personal-password"' in search.text

    inline = {"place": "inline", "form_id": "provider-edit-form-amazon_personal"}
    refused = signed_in.post("/settings/sync/vault/unlock", data={**inline, "password": "wrong"})
    assert refused.status_code == 422 and 'name="place" value="inline"' in refused.text
    assert not connected.is_unlocked()

    unlocked = signed_in.post("/settings/sync/vault/unlock", data={**inline, "password": PASSWORD})
    assert "vault-unlocked" in unlocked.headers["hx-trigger"]
    assert connected.is_unlocked() and "Vault unlocked." in unlocked.text
    assert 'id="vault-panel"' in unlocked.text and "Lock" in unlocked.text
