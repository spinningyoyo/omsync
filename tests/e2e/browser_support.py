"""The browser journeys' app and checks.

Each journey gets its own copy of the demo's made-up year, served as an ordinary ledger by a
background uvicorn thread on a free local port. The owner check and the vault are the fakes from
web_support, and sync is off, so no journey shows a system prompt, runs `bw`, or signs in to a
real site. A journey uses the app through Chromium, the way the owner does: it finds controls by
role and the words on screen, checks the result, reloads, and checks it again.
"""

import re
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from playwright.sync_api import Locator, Page, expect
from web_support import EMAIL, FakeOwnerCheck, FakeVault

from omsync.core.settings import Settings
from omsync.demo.build import build_demo
from omsync.web.app import create_app
from omsync.web.sync_runner import SyncRefused

SYNC_OFF = "Sync is off in the browser journeys."


@dataclass(frozen=True)
class Live:
    app: FastAPI
    url: str
    vault: FakeVault


def build_pristine(root: Path) -> Path:
    data = root / "pristine"
    build_demo(data, today=date.today())
    return data


@contextmanager
def serve(pristine: Path, workdir: Path) -> Iterator[Live]:
    """A fresh copy of the demo data, served as a ledger until the block ends."""
    data = workdir / "data"
    shutil.copytree(pristine, data)
    vault = FakeVault()
    settings = Settings(data_dir=data, host="127.0.0.1", email=EMAIL, vault="bitwarden")
    app = create_app(settings, vault=vault, owner_check=FakeOwnerCheck())
    app.state.vault_opener = lambda _settings: vault

    def sync_off(*_args: object, **_kwargs: object) -> None:
        raise SyncRefused(SYNC_OFF)

    app.state.sync_runner.start = sync_off
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, name="uvicorn-journey", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start for a browser journey")
        time.sleep(0.02)
    try:
        yield Live(app=app, url=f"http://127.0.0.1:{port}", vault=vault)
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def sign_in(page: Page, live: Live) -> None:
    page.add_init_script(
        "localStorage.setItem('omsync-privacy-mode', 'false');"
        "localStorage.setItem('omsync-theme', 'light');"
    )
    page.goto(f"{live.url}/sign-in")
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page).to_have_url(f"{live.url}/")


def settle(page: Page) -> None:
    """Wait until no htmx request is in flight."""
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => !document.querySelector('.htmx-request')")


def reload(page: Page) -> None:
    """Reload once the last write has answered, so the reload never cuts it off."""
    settle(page)
    page.reload()
    settle(page)


def undo(page: Page) -> None:
    """Click the toast's Undo and wait for its reply."""
    page.get_by_role("button", name="Undo").click()
    settle(page)


def unique(prefix: str) -> str:
    return f"{prefix} {time.monotonic_ns() % 100000}"


def expect_on_top(locator: Locator) -> None:
    """The element is inside the window, not clipped by an ancestor, and not covered: the point at
    its center belongs to it. A menu cut off by its list, or a form behind a modal, fails here."""
    expect(locator).to_be_visible()
    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()
    assert box is not None, "the element has no box"
    viewport = locator.page.viewport_size or {"width": 0, "height": 0}
    assert box["x"] >= 0 and box["y"] >= 0, f"the element starts outside the window: {box}"
    assert box["x"] + box["width"] <= viewport["width"] + 1, f"it runs off the right: {box}"
    assert box["y"] + box["height"] <= viewport["height"] + 1, f"it runs off the bottom: {box}"
    on_top = locator.evaluate(
        """el => {
            const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            let node = el;
            while (node) {
                const clip = getComputedStyle(node).overflow;
                if (node !== el && /hidden|clip|auto|scroll/.test(clip)) {
                    const p = node.getBoundingClientRect();
                    if (r.top < p.top - 1 || r.bottom > p.bottom + 1 ||
                        r.left < p.left - 1 || r.right > p.right + 1) return "clipped";
                }
                node = node.parentElement;
            }
            return hit && (el === hit || el.contains(hit)) ? "ok" : "covered";
        }"""
    )
    assert on_top == "ok", f"the element is {on_top}"


def name(text: str) -> re.Pattern[str]:
    """An accessible name that starts with text, for names that carry counts or amounts."""
    return re.compile("^" + re.escape(text))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
