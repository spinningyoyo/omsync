"""Take the screenshots the in-app Guides show, in light and dark, from the demo.

    uv run python scripts/guide-screenshots.py

Starts `omsync serve --demo` on a free port, with OMSYNC_DATA_DIR and OMSYNC_CONFIG in a new
temporary directory so no ledger is touched. It signs in with "Explore the demo" in headless Chrome
at 1280 px wide, sets the theme in localStorage, and writes
src/omsync/web/static/img/guides/<name>-light.png and -dark.png for each figure. Then it stops the
server by the process group it created, and removes the temporary directory. Rerun it when the
demo or a guide's figures change; the images are committed, so the guides show them without a run.
"""

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "src" / "omsync" / "web" / "static" / "img" / "guides"
WIDTH = 1280
THEMES = ("light", "dark")
HIDE_SCROLLBARS = (
    "const style = document.createElement('style');"
    "style.textContent = '*::-webkit-scrollbar{display:none!important}"
    "*{scrollbar-width:none!important}';"
    "document.head.appendChild(style);"
)


SETTINGS_CONTENT = 'nav[aria-label="Settings"] + div'


@dataclass(frozen=True)
class Shot:
    """One figure: the page to load, the element to crop to (None for the whole window), the
    window height, whether the accounts panel is open, and an optional action to run after the
    page loads (it may navigate, so the layout is set afterwards)."""

    name: str
    path: str
    selector: str | None
    height: int = 800
    accounts: bool = False
    before: Callable[[WebDriver], None] | None = None


SHOTS = (
    Shot("tour-home", "/", None, accounts=True),
    Shot("statements-bank-sync", "/settings/sync", SETTINGS_CONTENT),
    Shot("categorize-rules", "/settings/categories?tab=rules", SETTINGS_CONTENT, height=900),
    Shot(
        "reconcile-page",
        f"/reconcile?start={date.today().year}-01-01&end={date.today()}",
        "main",
        height=600,
    ),
    Shot("plan-budget", "/plan/budget", "main", height=900),
    Shot("recurring-page", "/recurring", "main", height=900),
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(data_dir: Path, port: int, log_path: Path) -> subprocess.Popen:
    """Start `omsync serve --demo` in its own process group, with its data in the temporary
    directory. The log is kept there so a failed start can be read."""
    env = {
        **os.environ,
        "OMSYNC_DATA_DIR": str(data_dir),
        "OMSYNC_CONFIG": str(data_dir.parent / "omsync.toml"),
        "OMSYNC_PORT": str(port),
    }
    with log_path.open("w") as log:
        return subprocess.Popen(
            ["uv", "run", "omsync", "serve", "--demo"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def wait_for_health(
    proc: subprocess.Popen, base: str, log_path: Path, timeout: float = 240
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"omsync serve exited early:\n{log_path.read_text()[-2000:]}")
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"omsync serve did not answer /health in {timeout:.0f}s")


def stop_server(proc: subprocess.Popen) -> None:
    """Stop the process group that start_server created. Never matches other processes by name."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def new_browser() -> WebDriver:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--hide-scrollbars")
    options.add_argument("--force-device-scale-factor=1")
    options.add_argument(f"--window-size={WIDTH},800")
    return webdriver.Chrome(options=options)


def sign_in(driver: WebDriver, base: str) -> None:
    driver.get(f"{base}/sign-in")
    driver.find_element(By.XPATH, "//button[contains(., 'Explore the demo')]").click()
    WebDriverWait(driver, 30).until(lambda d: "/sign-in" not in d.current_url)


def wait_ready(driver: WebDriver) -> None:
    WebDriverWait(driver, 30).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    time.sleep(0.8)


def load(driver: WebDriver, url: str, theme: str) -> None:
    """Load a page with the theme and privacy mode set, then wait until it has settled."""
    driver.get(url)
    driver.execute_script(
        "localStorage.setItem('omsync-theme', arguments[0]);"
        "localStorage.setItem('omsync-privacy-mode', 'false');",
        theme,
    )
    driver.get(url)
    wait_ready(driver)


def set_height(driver: WebDriver, height: int) -> None:
    """Make the viewport exactly `height` pixels tall: the browser's own chrome is measured once."""
    driver.set_window_size(WIDTH, height)
    inner = driver.execute_script("return window.innerHeight")
    if inner != height:
        driver.set_window_size(WIDTH, height + (height - inner))
    time.sleep(0.4)


def set_accounts_panel(driver: WebDriver, open_: bool) -> None:
    """Open or close the accounts panel by its toggle, so the shot shows it only when asked."""
    toggle = driver.find_element(By.CSS_SELECTOR, "[data-accounts-panel-target='toggle']")
    if (toggle.get_attribute("aria-expanded") == "true") != open_:
        toggle.click()
        time.sleep(0.5)


def capture(driver: WebDriver, shot: Shot, theme: str, base: str) -> Path:
    load(driver, base + shot.path, theme)
    if shot.before is not None:
        shot.before(driver)
    set_height(driver, shot.height)
    set_accounts_panel(driver, shot.accounts)
    driver.execute_script(HIDE_SCROLLBARS)
    target = OUT_DIR / f"{shot.name}-{theme}.png"
    if shot.selector is None:
        driver.save_screenshot(str(target))
    else:
        driver.execute_script("document.querySelector('main').scrollTop = 0")
        driver.find_element(By.CSS_SELECTOR, shot.selector).screenshot(str(target))
    return target


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="omsync-guides-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = tmp / "serve.log"
    proc = start_server(tmp / "data", port, log_path)
    driver = None
    written: list[Path] = []
    try:
        print(f"Starting the demo on port {port}; this builds it first.")
        wait_for_health(proc, base, log_path)
        driver = new_browser()
        sign_in(driver, base)
        for shot in SHOTS:
            for theme in THEMES:
                path = capture(driver, shot, theme, base)
                written.append(path)
                print(f"{path.name}: {path.stat().st_size // 1024} KB")
    finally:
        if driver is not None:
            driver.quit()
        stop_server(proc)
        shutil.rmtree(tmp, ignore_errors=True)
    total = sum(path.stat().st_size for path in written)
    print(f"{len(written)} images, {total // 1024} KB in total")


if __name__ == "__main__":
    main()
