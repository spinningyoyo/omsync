"""Chrome sessions, page requests, and waits."""

import contextlib
import platform
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, ClassVar

from selenium import webdriver
from selenium.common.exceptions import (
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from omsync.ingest.connectors.base import ConnectorError

BROWSER_MODES = ("chrome", "chrome-headless", "chrome-attach", "chrome-manual")
ATTACH_PORT = 9222
ATTACH_ADDRESS = f"127.0.0.1:{ATTACH_PORT}"


class BrowserError(ConnectorError):
    """Chrome could not be started or attached. The message names the fix and holds no secrets."""


def build_manual_chrome_debug_command(port: int, profile_dir: Path) -> str:
    """The command that starts Chrome for chrome-manual, shown when it is not running."""
    sysname = platform.system().lower()
    if "darwin" in sysname:
        chrome_bin = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    elif "windows" in sysname:
        chrome_bin = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    else:
        chrome_bin = "google-chrome"
    return f'"{chrome_bin}" --remote-debugging-port={port} --user-data-dir="{profile_dir}"'


class BrowserSession:
    """Chrome for one fetch. Call prepare() with the download and profile directories first."""

    # A Chrome left open after a failed sync, for the person to look at. The next browser started
    # by any connector closes it first, since it holds the debugging port.
    _left_open: ClassVar[subprocess.Popen[bytes] | None] = None

    def __init__(self) -> None:
        self.driver: Any = None
        self._browser_name = "chrome-attach"
        self._proc: subprocess.Popen[bytes] | None = None
        self._download_dir: Path | None = None
        self._profile_dir: Path | None = None
        self._debug_port: int | None = None

    def prepare(self, *, download_dir: Path, profile_dir: Path) -> None:
        self._download_dir = download_dir
        self._profile_dir = profile_dir

    def scratch_dir(self) -> Path:
        """The fresh download directory for this fetch."""
        if self._download_dir is None:
            raise BrowserError("prepare() must run before the browser is used")
        return self._download_dir

    def get_driver(self, browser_name: str, useragent: str | None = None) -> Any:
        """Start Chrome in one of the BROWSER_MODES and return its WebDriver."""
        if browser_name not in BROWSER_MODES:
            raise ValueError(f"browser type must be one of {', '.join(BROWSER_MODES)}")
        if self._profile_dir is None:
            raise BrowserError("prepare() must run before the browser is started")
        self._browser_name = browser_name
        self._close_left_open()
        download_dir = self.scratch_dir()
        profile_dir = self._profile_dir

        if browser_name in ("chrome", "chrome-headless", "chrome-attach"):
            chrome_options = Options()
            if browser_name == "chrome-headless":
                chrome_options.add_argument("--headless")
            if useragent:
                chrome_options.add_argument(f"--user-agent={useragent}")
            prefs = {
                "download.default_directory": str(download_dir),
                "credentials_enable_service": False,
                # Disable the save-password popup, which interferes with automation.
                "profile.password_manager_enabled": False,
            }
            chrome_options.add_experimental_option("prefs", prefs)

            if browser_name == "chrome-attach":
                # omsync launches Chrome with the profile, then Selenium attaches to it. Chrome
                # picks a free port for each run, so a second Chrome cannot take the port first.
                self._proc = self._start_debug_chrome(port=0)
                chrome_options.debugger_address = f"127.0.0.1:{self._debug_port}"
            else:
                chrome_options.add_argument(f"--user-data-dir={profile_dir}")

            driver = webdriver.Chrome(options=chrome_options, service=Service())
            if browser_name != "chrome-headless":
                driver.set_window_size(1920, 1080)
            driver.implicitly_wait(2)

            if browser_name == "chrome-attach":
                driver.execute_cdp_cmd(
                    "Browser.setDownloadBehavior",
                    {"behavior": "allow", "downloadPath": str(download_dir)},
                )
        else:
            # Attach to a Chrome the person started with --remote-debugging-port. This does
            # not launch Chrome, and close() leaves the browser open.
            try:
                self._wait_for_port("127.0.0.1", ATTACH_PORT, timeout=5.0)
            except BrowserError:
                command = build_manual_chrome_debug_command(ATTACH_PORT, profile_dir)
                raise BrowserError(
                    "Chrome is not listening for debugging on port "
                    f"{ATTACH_PORT}. Chrome 136+ ignores that port on the default profile. "
                    f"Start Chrome with a separate profile, for example: {command}"
                ) from None
            chrome_options = Options()
            chrome_options.debugger_address = ATTACH_ADDRESS
            driver = webdriver.Chrome(options=chrome_options, service=Service())
            self._stabilize_manual_chrome_target(driver)
            driver.implicitly_wait(2)

        self.driver = driver
        return driver

    def _stabilize_manual_chrome_target(self, driver: Any) -> None:
        """Switch away from dead or transient Chrome UI targets after a manual attach.

        Manual DevTools attach can land on ephemeral targets like the omnibox popup. Prefer a live
        http(s) page, but at minimum end up on any live handle.
        """
        live_fallback_handle = None
        for handle in list(driver.window_handles):
            try:
                driver.switch_to.window(handle)
                current_url = driver.current_url or ""
            except WebDriverException:
                continue  # a dead target; skip it
            if live_fallback_handle is None:
                live_fallback_handle = handle
            if current_url.startswith(("http://", "https://")):
                return
        if live_fallback_handle is not None:
            driver.switch_to.window(live_fallback_handle)

    def _wait_for_port(self, host: str, port: int, timeout: float = 10.0) -> None:
        start = time.time()
        last_err: OSError | None = None
        while time.time() - start < timeout:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return
            except OSError as exc:
                last_err = exc
                time.sleep(0.2)
        raise BrowserError(f"Chrome debugging port {host}:{port} not reachable ({last_err})")

    def _wait_for_chosen_port(self, proc: subprocess.Popen[bytes], profile_dir: Path) -> int:
        """The port Chrome chose for --remote-debugging-port=0. Chrome writes it to the profile's
        DevToolsActivePort file, first line, once the port is open."""
        marker = profile_dir / "DevToolsActivePort"
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                break  # Chrome handed the profile to a running Chrome and exited, or crashed
            try:
                first_line = marker.read_text().splitlines()[0]
                port = int(first_line)
            except (OSError, IndexError, ValueError):
                time.sleep(0.2)
                continue
            self._wait_for_port("127.0.0.1", port, timeout=5)
            return port
        raise BrowserError(
            "Chrome started but did not open its debugging port; close other Chrome "
            "windows that use omsync's browser profile, then sync again."
        )

    def _start_debug_chrome(self, port: int = 0) -> subprocess.Popen[bytes]:
        sysname = platform.system().lower()
        if "darwin" in sysname:
            cmd = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        elif "windows" in sysname:
            chrome_bin = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            if not Path(chrome_bin).exists():
                chrome_bin = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
            cmd = [chrome_bin]
        else:
            cmd = ["google-chrome"]

        profile_dir = self._profile_dir
        if profile_dir is None:
            raise BrowserError("prepare() must run before Chrome is launched")
        profile_dir.mkdir(parents=True, exist_ok=True)
        # A file left by an earlier Chrome would name a port that is closed now.
        (profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
        cmd += [
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            f"--download-dir={self.scratch_dir()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-backgrounding-occluded-windows",
            "about:blank",
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            raise BrowserError("Chrome could not be started; is it installed?") from None
        try:
            self._debug_port = self._wait_for_chosen_port(proc, profile_dir)
        except BrowserError:
            proc.terminate()
            raise
        return proc

    def leave_open(self) -> None:
        """Disconnect from the browser but leave it running on the page it is on, so the person can
        look at it. Only a Chrome that omsync launched itself can be left; any other is closed.
        The next browser started closes it."""
        if self._browser_name != "chrome-attach" or self._proc is None or self.driver is None:
            self.close()
            return
        driver, process = self.driver, self._proc
        self.driver = None
        self._proc = None
        with contextlib.suppress(Exception):
            driver.quit()  # ends the driver only: an attached Chrome keeps running
        BrowserSession._left_open = process

    @staticmethod
    def _close_left_open() -> None:
        process, BrowserSession._left_open = BrowserSession._left_open, None
        if process is not None:
            with contextlib.suppress(OSError):
                process.terminate()

    def close(self) -> None:
        """Close the browser this session started. A chrome-manual browser is left running."""
        driver = self.driver
        self.driver = None
        if driver is None:
            return
        # Teardown is best effort: an error here must not replace the fetch's result or error.
        try:
            if self._browser_name == "chrome-manual":
                # Chrome was launched by the person. Stop only the driver, not the browser.
                with contextlib.suppress(Exception):
                    driver.service.stop()
            else:
                with contextlib.suppress(Exception):
                    driver.close()
                with contextlib.suppress(Exception):
                    driver.quit()
        finally:
            if self._proc is not None:
                with contextlib.suppress(OSError):
                    self._proc.terminate()
                self._proc = None

    def wait(self, timeout: float = 20) -> WebDriverWait:
        return WebDriverWait(self.driver, timeout)

    def doc_readystate_is_complete(self, _foo: object = None) -> bool:
        """True once document.readyState is 'complete'. The argument is for wait functions."""
        return self.driver.execute_script("return document.readyState") == "complete"

    def wait_for_ajax_load(self, timeout: float = 20) -> bool:
        """Wait until the page has finished loading."""
        WebDriverWait(self.driver, timeout).until(self.doc_readystate_is_complete)
        return True

    def send_keys_to_element(self, webelement: Any, keys: str, check: bool = True) -> Any:
        """Type into a field, clearing it first, and optionally check the text arrived."""
        field = self.wait().until(EC.element_to_be_clickable(webelement))
        if field.get_attribute("value"):
            # Select all (CMD+A on macOS, CTRL+A elsewhere), then delete
            for cmd_key in [Keys.COMMAND, Keys.CONTROL]:
                field.send_keys(cmd_key, "a")
                field.send_keys(Keys.DELETE)
                if not field.get_attribute("value"):
                    break
        field.send_keys(keys)
        if check:
            self.wait().until(EC.text_to_be_present_in_element_value(webelement, keys))
        self.wait_for_ajax_load()
        return field

    def click_element(self, webelement: Any, timeout: float = 10) -> Any:
        """Click an element when it is clickable, then wait for the page to finish loading."""
        element = self.wait(timeout).until(EC.element_to_be_clickable(webelement))
        element.click()
        self.wait_for_ajax_load()
        return element

    def page_has(self, css: str) -> bool:
        """Whether the page has an element now. A script, because a find waits the implicit wait
        for an element that is not there, and this is asked many times a second."""
        return self.driver.execute_script(
            "return document.querySelector(arguments[0]) !== null", css
        )

    def xhr_request(
        self, method: str, url: str, body: str | None = None, headers: dict[str, str] | None = None
    ) -> str:
        """A synchronous request inside the browser, with the session's cookies even when url is on
        another of the bank's hosts. Values reach the page as script arguments, never as source."""
        script = (
            "var xhr = new XMLHttpRequest(); xhr.open(arguments[0], arguments[1], false); "
            "xhr.withCredentials = true; "
            "for (var [key, value] of Object.entries(arguments[3])) "
            "{ xhr.setRequestHeader(key, value); } "
            "xhr.send(arguments[2]); return xhr.responseText;"
        )
        return self.driver.execute_script(script, method, url, body, headers or {})

    def base_url(self) -> str:
        """The scheme and host of the current page."""
        parse = urllib.parse.urlparse(self.driver.current_url)
        return f"{parse.scheme}://{parse.netloc}"
