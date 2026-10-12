"""Shared institution sign-in hooks and completed-download storage.

Subclasses register themselves; keeping this base unregistered avoids a registry import cycle.
"""

import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By

from omsync.core.settings import load_settings
from omsync.core.vault import LoginCredentials, Vault
from omsync.ingest.connectors.banks.browser import BrowserSession
from omsync.ingest.connectors.base import (
    ConnectorAccount,
    ConnectorError,
    DateRange,
    NeedsUserAction,
    PartialFetch,
    Prompt,
)

STEP_PAUSE = 3
FOLDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # the drop folder's naming rule
logger = logging.getLogger(__name__)


def _web_address(uri: str, display_name: str) -> str:
    """The item's URI as an http(s) address. A URI with no scheme is taken as https. Anything else
    is refused, and the message does not echo the URI."""
    candidate = uri.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parts = urlsplit(candidate)
        _ = parts.port  # raises ValueError for a port that is not a number in range
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConnectorError(
            f"the Bitwarden item's first URI for {display_name} is not a web address"
        )
    return candidate


def _without_query(url: str) -> str:
    """scheme://host[:port]/path, with any userinfo, query, and fragment left out, for the log."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"


def last_four(statement_account_id: str) -> str:
    """The last four characters of a statement account ID, which is what the sites show."""
    return statement_account_id[-4:]


def site_last4(account: ConnectorAccount) -> str:
    """Resolve the account's site label from its statement alias, then its display last four."""
    if account.statement_account_id:
        return last_four(account.statement_account_id)
    if not account.last4:
        raise ConnectorError(
            "this account has no last four digits and no statement account ID, so the bank "
            "cannot find it; set its last four digits"
        )
    return account.last4


def folder_for(account: ConnectorAccount) -> str:
    """The subfolder that holds an account's files: its statement account ID, or its last4 when
    it has none. Import maps the folder name to the account, so the two must match its alias."""
    return account.statement_account_id or site_last4(account)


@dataclass(frozen=True)
class Download:
    """A finished file in the fetch's scratch directory, and where it is kept under statements/."""

    source: Path
    stem: str
    suffix: str
    subfolder: str | None = None


class BankConnector(BrowserSession):
    """One bank's website. Subclasses are registered; this base is not."""

    key: str = ""
    label: str = ""
    display_name: str = ""
    # True for an institution whose files are named by last four digits (BoA's CSV folders).
    last4_is_statement_id: bool = False
    fallback_sign_in_url: str = ""
    username_id: str = ""
    password_id: str = ""
    login_id: str = ""
    frame: str = ""
    url_auth: str = ""
    url_security: str = ""
    search_by: str = By.ID
    browser_mode: str = "chrome-attach"
    # How many digits the bank's one-time code has. A connector declares it when it is not 6, so
    # the Bank sync page sends the code once that many digits are entered.
    code_digits: int = 6

    def __init__(self) -> None:
        super().__init__()
        self._accounts: list[ConnectorAccount] = []
        self._login: LoginCredentials | None = None
        self._sign_in_url = ""
        self._prompt: Prompt | None = None
        self._period = DateRange(start=date.today(), end=date.today())
        self._todate = datetime.now()
        self.fromdate = datetime.now()
        self._downloads: list[Download] = []
        self._skipped: list[str] = []

    @property
    def todate(self) -> datetime:
        """The end of the window, never later than now."""
        return self._todate if self._todate < datetime.now() else datetime.now()

    @todate.setter
    def todate(self, value: datetime) -> None:
        self._todate = value

    def fetch(
        self,
        accounts: list[ConnectorAccount],
        period: DateRange,
        vault: Vault,
        prompt: Prompt,
        dest: Path,
        *,
        vault_item_id: str | None,
        close_on_failure: bool = True,
    ) -> list[Path]:
        if vault_item_id is None:
            raise ConnectorError(
                f"no Bitwarden login is set for {self.display_name}; choose one on the "
                "Bank sync page"
            )
        login = vault.get_login(vault_item_id)
        if not login.username:
            raise ConnectorError(f"the Bitwarden login for {self.display_name} has no username")

        self._accounts = list(accounts)
        self._login = login
        self._sign_in_url = self._resolve_sign_in_url(login)
        self._prompt = prompt
        self._period = period
        self._todate = datetime.combine(period.end, datetime.min.time())
        self.fromdate = datetime.combine(period.start, datetime.min.time())
        self._downloads = []
        self._skipped = []

        # A fresh scratch directory per fetch. It is deleted when this block ends, after the
        # finished files have been moved into dest.
        with tempfile.TemporaryDirectory(prefix="omsync-bank-") as scratch:
            self.prepare(download_dir=Path(scratch), profile_dir=self._profile_from_settings())
            failed = True  # a browser left open for the person is one a fetch failed in
            try:
                self._sign_in_and_download()
                failed = bool(self._skipped)
            except TimeoutException:
                raise NeedsUserAction(
                    f"{self.display_name} did not respond in time; sync again"
                ) from None
            except WebDriverException as exc:
                raise ConnectorError(
                    f"the browser stopped while syncing {self.display_name} ({type(exc).__name__})"
                ) from None
            finally:
                if failed and not close_on_failure:
                    self.leave_open()
                else:
                    self.close()
            stored = self._store(dest)
            if self._skipped:
                raise PartialFetch("; ".join(self._skipped), stored)
            return stored

    def _profile_from_settings(self) -> Path:
        # The same omsync.toml and OMSYNC_* settings the CLI reads.
        return load_settings().browser_profile_dir

    def _resolve_sign_in_url(self, login: LoginCredentials) -> str:
        """The sign-in page: the item's first URI when it has one, otherwise the built-in fallback.

        Validate the selected URI before starting a browser that will receive credentials.
        """
        if login.uris:
            url = _web_address(login.uris[0], self.display_name)
            source = "the first URI of the Bitwarden item"
        else:
            url = self.fallback_sign_in_url
            source = "the built-in sign-in URL"
        logger.debug("%s: sign-in page from %s: %s", self.key, source, _without_query(url))
        return url

    def _sign_in_and_download(self) -> None:
        self.get_driver(self.browser_mode)
        self.login()
        time.sleep(STEP_PAUSE)
        self.captcha()
        time.sleep(STEP_PAUSE)
        if self.request_auth():
            self.authenticate(self._ask_code())
            self.close_popup()
        else:
            time.sleep(STEP_PAUSE)
            self.close_popup()
            time.sleep(STEP_PAUSE)
        self.get_account_activity()

    def _ask_code(self) -> str:
        """Ask for the code the bank sent. A timeout or an empty answer needs the person."""
        if self._prompt is None:
            raise RuntimeError("fetch() must set the prompt first")
        try:
            answer = self._prompt.ask(
                f"Enter the code {self.display_name} sent you",
                secret=False,
                digits=self.code_digits,
            )
        except TimeoutError:
            raise NeedsUserAction(
                f"no code came in time for {self.display_name}; sync again"
            ) from None
        code = (answer or "").strip()
        if not code:
            raise NeedsUserAction(f"no code was entered for {self.display_name}; sync again")
        return code

    def _credentials(self) -> LoginCredentials:
        if self._login is None:
            raise RuntimeError("fetch() must load the login first")
        return self._login

    # Hooks. The defaults suit a bank with no captcha, no MFA, and no pop-ups.

    def login(self) -> None:
        """Automates login using configured credentials and web elements."""
        self.driver.get(self._sign_in_url)
        time.sleep(3)  # Wait arbitrary for 3 seconds for webpage to load
        self.wait_for_ajax_load()
        if self.frame:
            self.driver.switch_to.frame(self.frame)
        credentials = self._credentials()
        self.send_keys_to_element((self.search_by, self.username_id), credentials.username or "")
        self.send_keys_to_element((self.search_by, self.password_id), credentials.password)
        login_button = self.click_element((self.search_by, self.login_id))
        self.wait_for_page_load(login_button)

    def captcha(self) -> None:
        """Hook for banks that show a CAPTCHA. Raises NeedsUserAction when one is shown."""

    def request_auth(self, timeout: float = 5) -> bool:
        """Hook: True when the bank is asking for an MFA code, after sending the request."""
        return False

    def authenticate(self, code: str) -> None:
        """Hook: type the MFA code and continue."""

    def close_popup(self, timeout: float = 3) -> None:
        """Hook for modal pop-ups after sign-in."""

    def get_account_activity(self) -> None:
        """Hook: open each account and keep its statement files."""
        raise NotImplementedError

    # Keeping files.

    def skip_account(self, last4: str, reason: str) -> None:
        """Record that the bank gave nothing usable for one account. The other accounts are still
        fetched and kept, and the sync reports this one as failed."""
        self._skipped.append(f"{self.display_name} gave nothing for ••{last4}: {reason}")

    def keep_file(
        self, source: Path, *, stem: str, suffix: str, subfolder: str | None = None
    ) -> None:
        """Keep a finished download. subfolder is a statement account ID, or None for the top.

        The file is moved aside at once. A bank names every download alike (BoA's is always
        stmt.csv), and Chrome saves the next one over a file of the same name."""
        self._check_subfolder(subfolder)
        kept = self.scratch_dir() / f"download-{len(self._downloads)}{suffix}"
        source.replace(kept)
        self._downloads.append(Download(kept, stem, suffix, subfolder))

    def keep_text(self, text: str, *, stem: str, suffix: str, subfolder: str | None = None) -> None:
        """Keep text a page returned, such as an XHR response, as a file."""
        self._check_subfolder(subfolder)
        path = self.scratch_dir() / f"response-{len(self._downloads)}{suffix}"
        path.write_text(text, encoding="utf-8")
        self._downloads.append(Download(path, stem, suffix, subfolder))

    def require_download(self, path: Path | None) -> Path:
        if path is None:
            raise NeedsUserAction(
                f"{self.display_name} did not finish a download in time; sync again"
            )
        return path

    def stem_for(self, last4: str, number: int | None = None) -> str:
        """A file name with the last four digits and the window, never a full account number."""
        stem = f"{self.key}_{last4}_{self._period.start:%Y%m%d}_to_{self._period.end:%Y%m%d}"
        return stem if number is None else f"{stem}_{number}"

    def account_not_shown(self, last4: str) -> ConnectorError:
        return ConnectorError(
            f"{self.display_name} did not show the account ••{last4}. If its card was reissued, "
            "give the account the new card's last four with `omsync accounts alias`"
        )

    @staticmethod
    def _check_subfolder(subfolder: str | None) -> None:
        if subfolder is not None and not FOLDER_NAME.fullmatch(subfolder):
            raise ConnectorError("a statement account ID cannot name a folder")

    def _store(self, dest: Path) -> list[Path]:
        """Move each kept file into dest, or dest/<subfolder>, under a name that is never taken."""
        stored: list[Path] = []
        for item in self._downloads:
            folder = dest if item.subfolder is None else dest / item.subfolder
            folder.mkdir(parents=True, exist_ok=True)
            target = _free_name(folder, f"{item.stem}{item.suffix}")
            shutil.move(str(item.source), str(target))
            stored.append(target)
        return stored


def _free_name(folder: Path, name: str) -> Path:
    """The path for name in folder, with -1, -2, and so on before the suffix if it is taken."""
    candidate = folder / name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while candidate.exists():
        candidate = folder / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate
