"""Browser smoke test: signing in lands on Home.

The app runs in a background uvicorn thread on a free local port. Its data directory is temporary
and its vault is a fake, so no test touches bw, the keychain, or the real data directory."""

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Page, expect

import omsync.ledger.models  # noqa: F401  registers every table on Base.metadata
from omsync.core.db import Base
from omsync.core.settings import Settings
from omsync.core.vault import VaultError
from omsync.web.app import create_app

pytestmark = pytest.mark.e2e

EMAIL = "ana@example.test"
PASSWORD = "correct-horse-battery"


class FakeVault:
    """Accepts one email and password. Records lock calls. Never runs bw."""

    idle_minutes = 15

    def __init__(self) -> None:
        self.unlocked = False

    def unlock(self, email: str, password: str) -> None:
        if email != EMAIL or password != PASSWORD:
            raise VaultError("Incorrect master password")
        self.unlocked = True

    def lock(self) -> None:
        self.unlocked = False

    def is_unlocked(self) -> bool:
        return self.unlocked


@dataclass(frozen=True)
class LiveApp:
    app: FastAPI
    url: str


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def live_app(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveApp]:
    data_dir: Path = tmp_path_factory.mktemp("omsync-data")
    app = create_app(Settings(data_dir=data_dir), vault=FakeVault())
    Base.metadata.create_all(app.state.engine)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="uvicorn-e2e-shell", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start for the sign-in and shell tests")
        time.sleep(0.02)
    try:
        yield LiveApp(app=app, url=f"http://127.0.0.1:{port}")
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _sign_in(page: Page, live: LiveApp) -> None:
    page.goto(f"{live.url}/sign-in")
    page.get_by_label("Email").fill(EMAIL)
    page.get_by_label("Password", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page).to_have_url(f"{live.url}/")


def test_sign_in_lands_on_home(page: Page, live_app: LiveApp) -> None:
    _sign_in(page, live_app)

    expect(page.get_by_role("heading", name="Home", level=1)).to_be_visible()
    expect(page.get_by_role("heading", name="Welcome to omsync")).to_be_visible()
