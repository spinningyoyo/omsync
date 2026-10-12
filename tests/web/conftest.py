"""Shared fixtures for the web tests: a fake vault, an app on a temporary data directory, and seeded
ledger rows. Nothing here runs `bw`, reads the keychain, or touches the real data directory."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from support import FakeVault, MakeClient, sign_in

import omsync.ledger.models  # noqa: F401  registers every table on Base.metadata
from omsync.core.db import Base
from omsync.core.sessions import SessionStore
from omsync.core.settings import Settings
from omsync.web.app import create_app


@pytest.fixture
def vault() -> FakeVault:
    return FakeVault()


@pytest.fixture
def make_client(tmp_path: Path, vault: FakeVault) -> MakeClient:
    """Build a client on a fresh app with the schema created in a temporary database."""

    def build(sessions: SessionStore | None = None) -> TestClient:
        app = create_app(Settings(data_dir=tmp_path), vault=vault, sessions=sessions)
        Base.metadata.create_all(app.state.engine)
        return TestClient(app)

    return build


@pytest.fixture
def client(make_client: MakeClient) -> TestClient:
    return make_client()


@pytest.fixture
def app(client: TestClient) -> FastAPI:
    return client.app


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    sign_in(client)
    return client
