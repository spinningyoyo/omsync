"""Every test runs with a throwaway HOME, no OMSYNC_* variables, and no keychain, so nothing can
reach ~/omsync-data, ~/.config/omsync, or real credentials. A test that needs a secret
monkeypatches keyring itself, which overrides the guard.

The fixtures also build an empty ledger for the CLI tests, and an app on a temporary data
directory with a fake vault and a fake owner check for the HTTP tests. Nothing here runs `bw` or
shows a system prompt."""

import os
from pathlib import Path

import keyring
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ledgerkit import Ledger
from web_support import FakeOwnerCheck, FakeVault, MakeClient, sign_in

import omsync.ledger.models  # noqa: F401  registers every table on Base.metadata
from omsync.core.db import Base
from omsync.core.sessions import SessionStore
from omsync.core.settings import Settings
from omsync.web.app import create_app

_REAL_HOME = Path.home()
_PLAYWRIGHT_CACHES = (
    _REAL_HOME / "Library" / "Caches" / "ms-playwright",
    _REAL_HOME / ".cache" / "ms-playwright",
    _REAL_HOME / "AppData" / "Local" / "ms-playwright",
)


def _no_keychain(*args: object, **kwargs: object) -> None:
    raise RuntimeError("tests must not use the real keychain; monkeypatch keyring instead")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    # Tool caches keep their real locations, so uv and Playwright do not download again.
    if not os.environ.get("UV_CACHE_DIR"):
        monkeypatch.setenv("UV_CACHE_DIR", str(_REAL_HOME / ".cache" / "uv"))
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        for cache in _PLAYWRIGHT_CACHES:
            if cache.is_dir():
                monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
                break
    home = str(tmp_path_factory.mktemp("home"))
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)  # Path.home() on Windows
    for name in [name for name in os.environ if name.startswith("OMSYNC_")]:
        monkeypatch.delenv(name)
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, _no_keychain)


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    """An empty ledger in tmp_path. The CLI reads it through the environment."""
    data = tmp_path / "data"
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(data))
    monkeypatch.setenv("OMSYNC_CONFIG", str(tmp_path / "absent.toml"))
    return Ledger(data)


@pytest.fixture
def vault() -> FakeVault:
    return FakeVault()


@pytest.fixture
def owner_check() -> FakeOwnerCheck:
    return FakeOwnerCheck()


@pytest.fixture
def make_client(tmp_path: Path, vault: FakeVault, owner_check: FakeOwnerCheck) -> MakeClient:
    """Build a client on a fresh app with the schema created in a temporary database."""

    def build(sessions: SessionStore | None = None) -> TestClient:
        app = create_app(
            Settings(data_dir=tmp_path), vault=vault, owner_check=owner_check, sessions=sessions
        )
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


# The browser journeys (see browser_support.py). The demo is built once per run and copied for each
# journey, so no journey sees another's writes.


@pytest.fixture(scope="session")
def demo_pristine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from browser_support import build_pristine

    return build_pristine(tmp_path_factory.mktemp("demo"))


@pytest.fixture
def live(demo_pristine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The app the journeys drive. The keychain holds no vault API key, as on a new install."""
    from browser_support import serve

    monkeypatch.setattr(keyring, "get_password", lambda *args, **kwargs: None)
    with serve(demo_pristine, tmp_path) as app:
        yield app


@pytest.fixture
def user(page, live):
    """The browser, signed in to its own copy of the demo data, on Home."""
    from browser_support import sign_in as browser_sign_in

    browser_sign_in(page, live)
    return page
