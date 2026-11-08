"""Bank connector errors never carry a secret.

No network and no real browser. FakeChrome stands in for Selenium's WebDriver, and its page
elements are FakeElement, which Selenium's expected conditions accept unchanged. Waits check their
condition once instead of polling. Synthetic data only: fake names and account numbers."""

import json
import re
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement

from omsync.core.vault import LoginCredentials
from omsync.ingest.connectors.banks import browser as browser_module
from omsync.ingest.connectors.banks.base import BankConnector
from omsync.ingest.connectors.banks.boa import BoaConnector
from omsync.ingest.connectors.base import (
    ConnectorAccount,
    ConnectorError,
    DateRange,
)

ITEM_ID = "item-42"
USERNAME = "jane.tester@example.com"
PASSWORD = "pw-SECRET-7731"
FULL_ACCOUNT = "000012345678"
WINDOW = DateRange(start=date(2026, 1, 1), end=date(2026, 10, 1))
CODE = "123456"


class FastWait:
    """Stands in for WebDriverWait: the condition is checked once, and a false one times out."""

    def __init__(self, driver: object, timeout: float = 0, *args: Any, **kwargs: Any) -> None:
        self._driver = driver

    def until(self, method: Callable[[Any], Any], message: str = "") -> Any:
        try:
            value = method(self._driver)
        except NoSuchElementException:
            value = False
        if value:
            return value
        raise TimeoutException(message)


class FakeProcess:
    """The Chrome process omsync launches. Each one started is kept in PROCESSES."""

    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


PROCESSES: list[FakeProcess] = []


class FakeVault:
    """Connectors read the vault only through get_login. This records which item was asked for.
    uris are the item's URIs; an empty tuple is an item with none."""

    def __init__(self, uris: tuple[str, ...] = ()) -> None:
        self.uris = uris
        self.requested: list[str] = []

    def get_login(self, item_id: str) -> LoginCredentials:
        self.requested.append(item_id)
        return LoginCredentials(
            id=item_id,
            name="Bank login",
            username=USERNAME,
            password=PASSWORD,
            totp=None,
            uris=self.uris,
        )


class FakePrompt:
    """Answers the one question a flow asks, or raises the error it was given."""

    def __init__(self, answer: str = CODE, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.asked: list[tuple[str, bool]] = []
        self.digits: list[int | None] = []

    def ask(self, question: str, *, secret: bool = False, digits: int | None = None) -> str:
        self.asked.append((question, secret))
        self.digits.append(digits)
        if self.error is not None:
            raise self.error
        return self.answer

    def show(self, message: str, *, image: str | None = None) -> None:
        pass

    def cancelled(self) -> bool:
        return False


class FakeElement(WebElement):
    """A page element. It subclasses WebElement because Selenium checks for that type."""

    def __init__(
        self,
        *,
        attrs: dict[str, str] | None = None,
        text: str = "",
        on_click: Callable[[], None] | None = None,
        children: dict[tuple[str, str], list["FakeElement"]] | None = None,
    ) -> None:
        # No super().__init__. ActionChains reads the id, so the fake sets one of its own.
        self._id = f"fake-element-{id(self)}"
        self._attrs = dict(attrs or {})
        self._text = text
        self._on_click = on_click
        self._children = dict(children or {})
        self.value = ""
        self.clicks = 0
        self._modifier = False

    @property
    def text(self) -> str:
        return self._text

    def click(self) -> None:
        self.clicks += 1
        if self._on_click is not None:
            self._on_click()

    def send_keys(self, *values: str) -> None:
        for key in values:
            if key in (Keys.COMMAND, Keys.CONTROL):
                self._modifier = True
                continue
            if self._modifier and key == "a":
                self._modifier = False  # select all: nothing to do in a fake
                continue
            self._modifier = False
            if key == Keys.DELETE:
                self.value = ""
            else:
                self.value += key

    def clear(self) -> None:
        self.value = ""

    def get_attribute(self, name: str) -> str | None:
        if name == "value":
            # An option carries its value as an attribute; a field reports what was typed.
            return self._attrs["value"] if "value" in self._attrs else self.value
        if name == "textContent":
            return self._text
        return self._attrs.get(name)

    def is_displayed(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def find_element(self, by: str = By.ID, value: str | None = None) -> "FakeElement":
        found = self.find_elements(by, value)
        if not found:
            raise NoSuchElementException("no such element in the fake page")
        return found[0]

    def find_elements(self, by: str = By.ID, value: str | None = None) -> list["FakeElement"]:
        return list(self._children.get((by, value or ""), []))


class _Switch:
    def frame(self, _name: object) -> None:
        pass

    def window(self, _handle: object) -> None:
        pass


class FakeChrome:
    """One fake Chrome. The test fills its page; the connector drives it through WebDriver calls."""

    def __init__(self) -> None:
        self.page: dict[tuple[str, str], FakeElement | list[FakeElement]] = {}
        self.xhr: dict[str, str] = {}  # URL -> the response text for XHR GET and POST
        self.requests: list[tuple[str, str, str | None, dict[str, str]]] = []  # by xhr_request
        self.cookies: dict[str, str] = {}
        self.current_url = ""
        self.visited: list[str] = []  # every URL the connector asked the browser to open
        self.download_dir: Path | None = None
        self.cdp: list[tuple[str, dict[str, Any]]] = []
        self.window_handles: list[str] = []
        self.window_closed = False  # the connector closed the page's window
        self.switch_to = _Switch()
        self.quit_called = False

    def attach(self, options: Any) -> "FakeChrome":
        """Stands in for webdriver.Chrome(options=...). Reads the download folder from the prefs."""
        prefs = options.experimental_options.get("prefs", {})
        self.download_dir = Path(prefs["download.default_directory"])
        return self

    def get(self, url: str) -> None:
        self.visited.append(url)
        self.current_url = url

    def find_element(self, by: str = By.ID, value: str | None = None) -> FakeElement:
        found = self.find_elements(by, value)
        if not found:
            raise NoSuchElementException("no such element in the fake page")
        return found[0]

    def find_elements(self, by: str = By.ID, value: str | None = None) -> list[FakeElement]:
        found = self.page.get((by, value or ""), [])
        return list(found) if isinstance(found, list) else [found]

    def execute_script(self, script: str, *args: Any) -> Any:
        if "document.readyState" in script:
            return "complete"
        attribute = re.search(r"arguments\[0\]\.setAttribute\('([^']*)', '([^']*)'\)", script)
        if attribute:
            args[0]._attrs[attribute.group(1)] = attribute.group(2)
            return None
        if "xhr.open(arguments[0], arguments[1]" in script:
            method, url, body, headers = args
            self.requests.append((method, url, body, dict(headers)))
            return self.xhr.get(url.split("?")[0], "")
        match = re.search(r'xhr\.open\("(GET|POST)", "([^"]+)"', script)
        if match:
            method, url = match.group(1), match.group(2)
            if method == "GET":
                self.requests.append((method, url, None, {}))
            body = self.xhr.get(url, "")
            if method == "POST":
                return json.dumps({"resp": None, "respText": body, "status": 200, "headers": ""})
            return body
        return None

    def get_cookie(self, name: str) -> dict[str, str] | None:
        return {"value": self.cookies[name]} if name in self.cookies else None

    def execute_cdp_cmd(self, cmd: str, params: dict[str, Any]) -> None:
        self.cdp.append((cmd, params))

    def execute(self, command: str, params: object = None) -> dict[str, Any]:
        """ActionChains sends its moves here. The fake accepts them and does nothing."""
        return {"value": None}

    def set_window_size(self, width: int, height: int) -> None:
        pass

    def implicitly_wait(self, seconds: float) -> None:
        pass

    def close(self) -> None:
        self.window_closed = True

    def quit(self) -> None:
        self.quit_called = True


def el(**kwargs: Any) -> FakeElement:
    return FakeElement(**kwargs)


def go(chrome: FakeChrome, url: str) -> Callable[[], None]:
    """A click handler that moves the fake browser to url, as a sign-in button does."""

    def navigate() -> None:
        chrome.current_url = url

    return navigate


def account(
    statement_account_id: str, account_type: str = "checking", account_id: int = 1
) -> ConnectorAccount:
    return ConnectorAccount(
        account_id=account_id,
        last4=statement_account_id[-4:],
        type=account_type,
        statement_account_id=statement_account_id,
    )


def fetch(
    connector: BankConnector,
    accounts: list[ConnectorAccount],
    dest: Path,
    *,
    vault: FakeVault | None = None,
    prompt: FakePrompt | None = None,
    item: str | None = ITEM_ID,
    close_on_failure: bool = True,
) -> list[Path]:
    return connector.fetch(
        accounts,
        WINDOW,
        vault or FakeVault(),
        prompt or FakePrompt(),
        dest,
        vault_item_id=item,
        close_on_failure=close_on_failure,
    )


# Pages. Each builder fills the fake page for one bank's flow and returns the elements a test
# wants to inspect.


def boa_sign_in(
    chrome: FakeChrome, after_login: str = "https://secure.bankofamerica.com/accounts/checking"
) -> tuple[FakeElement, FakeElement]:
    user = el()
    password = el()
    chrome.page[(By.ID, "oid")] = user
    chrome.page[(By.ID, "pass")] = password
    chrome.page[(By.ID, "secure-signin-submit")] = el(on_click=go(chrome, after_login))
    return user, password


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the settings at a temp data dir, and never read the real config file."""
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMSYNC_CONFIG", str(tmp_path / "no-such-omsync.toml"))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(browser_module, "WebDriverWait", FastWait)


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that forgets the chrome fixture fails here, rather than starting a browser."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("tests must not start Chrome; use the chrome fixture")

    monkeypatch.setattr(webdriver, "Chrome", refuse)


@pytest.fixture(autouse=True)
def launches(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Records the profile each attach-mode launch was given. No Chrome process starts."""
    profiles: list[Path] = []
    PROCESSES.clear()
    monkeypatch.setattr(browser_module.BrowserSession, "_left_open", None)

    def fake_start(self: browser_module.BrowserSession, port: int) -> FakeProcess:
        profiles.append(self._profile_dir)  # type: ignore[arg-type]
        process = FakeProcess()
        PROCESSES.append(process)
        return process

    monkeypatch.setattr(browser_module.BrowserSession, "_start_debug_chrome", fake_start)
    return profiles


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> FakeChrome:
    site = FakeChrome()
    monkeypatch.setattr(
        webdriver, "Chrome", lambda options=None, service=None: site.attach(options)
    )
    return site


# Registration


# Profile and credentials


# MFA


# Captcha, timeouts, and messages


def test_error_messages_never_hold_the_password_username_or_full_account(
    chrome: FakeChrome, tmp_path: Path
) -> None:
    boa_sign_in(chrome)

    def browser_error() -> None:
        raise WebDriverException(f"login failed for {USERNAME} with {PASSWORD} at {FULL_ACCOUNT}")

    chrome.page[(By.ID, "secure-signin-submit")] = el(on_click=browser_error)

    with pytest.raises(ConnectorError) as info:
        fetch(BoaConnector(), [account(FULL_ACCOUNT)], tmp_path / "BOA")

    message = str(info.value)
    assert "WebDriverException" in message
    for secret in (USERNAME, PASSWORD, FULL_ACCOUNT):
        assert secret not in message


# Downloads


# Accounts with no statement account ID yet (5.3): they are found by last four digits.
