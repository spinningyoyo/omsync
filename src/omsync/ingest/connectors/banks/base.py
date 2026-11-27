"""Shared institution sign-in hooks and fetched-statement storage.

Subclasses register themselves; keeping this base unregistered avoids a registry import cycle.
"""

import logging
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from selenium.common.exceptions import JavascriptException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By

from omsync.core.settings import load_settings
from omsync.core.vault import LoginCredentials, Vault
from omsync.ingest.connectors.base import (
    ConnectorAccount,
    ConnectorError,
    DateRange,
    NeedsUserAction,
    PartialFetch,
    Prompt,
    SyncCancelled,
)
from omsync.ingest.connectors.browser import BrowserSession
from omsync.ingest.timing import timed

SIGN_IN_TIMEOUT = 60  # seconds for the bank to answer a sign-in or a code
APPROVAL_TIMEOUT = 300  # seconds for the person to approve a sign-in in the bank's app
APPROVAL_GONE = 15  # seconds the approval screen may be gone, signed out, before the wait ends
SIGN_IN_POLL = 0.25  # seconds between looks at the page
# Seconds between asks for the account list while signing in, and before the first. Signed in means
# the bank answers it: see docs/architecture/sync.md, "Signing in".
LIST_ASK_INTERVAL = 5
CODE_FAILED_TIMEOUT = 15  # seconds to see whether a code that hit an error was taken anyway
NOT_A_STATEMENT = "the bank answered with something other than a statement"
FOLDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # the drop folder's naming rule
logger = logging.getLogger(__name__)


class Check(StrEnum):
    """How the bank checks it is you, once a connector has asked it to: a code to type, or an
    approval in the bank's app, such as after scanning a QR."""

    code = "code"
    approval = "approval"


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
        raise ConnectorError(f"the vault item's first URI for {display_name} is not a web address")
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
class SiteAccount:
    """An account as a bank's site lists it. key is what the bank's requests call it. kind is the
    bank's own type code, for a bank whose requests differ by it."""

    last4: str
    key: str
    kind: str = ""


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
    browser_mode: str = "chrome-attach"
    # How many digits the bank's one-time code has. A connector declares it when it is not 6, so
    # the Data sync page sends the code once that many digits are entered.
    code_digits: int = 6
    statement_suffix: str = ".ofx"
    # How long the bank's code request must stay on the page before it counts, for a bank whose
    # request looks like an ordinary page while the signed-in page is still loading.
    mfa_settle: float = 0

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
        self._listed: list[SiteAccount] | None = None  # the list whose answer meant signed in

    @property
    def todate(self) -> datetime:
        """The end of the window, never later than now."""
        return min(datetime.now(), self._todate)

    @todate.setter
    def todate(self, value: datetime) -> None:
        self._todate = value

    def fetch(
        self,
        accounts: list[ConnectorAccount],
        period: DateRange,
        vault: Vault | None,
        prompt: Prompt,
        dest: Path,
        *,
        vault_item_id: str | None,
    ) -> list[Path]:
        login = None if vault is None else self._vault_login(vault, vault_item_id)
        self._accounts = list(accounts)
        self._login = login
        self._sign_in_url = (
            self.fallback_sign_in_url if login is None else self._resolve_sign_in_url(login)
        )
        self._prompt = prompt
        self._period = period
        self._todate = datetime.combine(period.end, datetime.min.time())
        self.fromdate = datetime.combine(period.start, datetime.min.time())
        self._downloads = []
        self._skipped = []
        self._listed = None

        # A fresh scratch directory per fetch. It is deleted when this block ends, after the
        # finished files have been moved into dest. A Chrome left open can still hold a file in it
        # on Windows; that must not replace the fetch's result or error.
        with tempfile.TemporaryDirectory(
            prefix="omsync-bank-", ignore_cleanup_errors=True
        ) as scratch:
            self.prepare(download_dir=Path(scratch), profile_dir=self._profile_from_settings())
            failed = True  # a browser left open for the person is one a fetch failed in
            try:
                self._sign_in_and_download()
                failed = bool(self._skipped)
            except SyncCancelled:
                failed = False  # a cancelled sync always closes its browser
                raise
            except TimeoutException:
                raise NeedsUserAction(
                    f"{self.display_name} did not respond in time; sync again"
                ) from None
            except WebDriverException as exc:
                detail = (exc.msg or "").splitlines()[0] if getattr(exc, "msg", None) else ""
                suffix = f": {detail}" if detail else ""
                raise ConnectorError(
                    f"the browser stopped while syncing {self.display_name} "
                    f"({type(exc).__name__}{suffix})"
                ) from None
            finally:
                if failed:
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

    def _vault_login(self, vault: Vault, vault_item_id: str | None) -> LoginCredentials:
        if vault_item_id is None:
            raise ConnectorError(
                f"no vault login is set for {self.display_name}; choose one on the Data sync page"
            )
        login = vault.get_login(vault_item_id)
        if not login.username:
            raise ConnectorError(f"the vault login for {self.display_name} has no username")
        return login

    def _resolve_sign_in_url(self, login: LoginCredentials) -> str:
        """The sign-in page: the item's first URI when it has one, otherwise the built-in fallback.

        Validate the selected URI before starting a browser that will receive credentials.
        """
        if login.uris:
            url = _web_address(login.uris[0], self.display_name)
            source = "the first URI of the vault item"
        else:
            url = self.fallback_sign_in_url
            source = "the built-in sign-in URL"
        logger.debug("%s: sign-in page from %s: %s", self.key, source, _without_query(url))
        return url

    def _sign_in_and_download(self) -> None:
        with timed(self.key, "sign-in"):
            self.get_driver(self.browser_mode)
            if self._login is None:
                self._manual_sign_in()
                needs_code = False
            else:
                self.login()
                needs_code = self._await_sign_in(watch_mfa=True)
        if needs_code:
            if self.request_auth() is Check.approval:
                self._await_approval()
            else:
                self._enter_code(self._ask_code())
        with timed(self.key, "download"):
            self.get_account_activity()

    def _manual_sign_in(self) -> None:
        """Open the bank's sign-in page and wait until the person says they signed in. The download
        then asks the bank for the account list itself."""
        self.driver.get(self._sign_in_url)
        signed_in = self._the_prompt().confirm(
            f"Sign in to {self.display_name} in the browser window, then select I have signed in.",
            button="I have signed in",
        )
        if not signed_in:
            raise NeedsUserAction(f"{self.display_name} was not signed in in time; sync again")

    def _enter_code(self, code: str) -> None:
        """Enter the code, then let the page say whether it worked. Submitting can navigate the
        page and raise an error when the code was taken, so an error only shortens the wait."""
        try:
            self.authenticate(code)
            failed = False
        except WebDriverException:
            failed = True
        timeout = CODE_FAILED_TIMEOUT if failed else SIGN_IN_TIMEOUT
        with timed(self.key, "sign-in after code"):
            try:
                self._await_sign_in(watch_mfa=False, timeout=timeout)
            except NeedsUserAction:
                if failed:
                    raise NeedsUserAction(
                        f"{self.display_name} did not take the code; sync again"
                    ) from None
                raise

    def _await_sign_in(self, *, watch_mfa: bool, timeout: float | None = None) -> bool:
        """Watch until the bank decides. False when the person is signed in, which is when the bank
        answers the account list, asked every LIST_ASK_INTERVAL seconds. True when the page asks for
        an identity check and the list is still refused. A page that never gets there, such as one
        that needs a person, ends this."""
        now = time.monotonic()
        deadline = now + (SIGN_IN_TIMEOUT if timeout is None else timeout)
        next_ask = now + LIST_ASK_INTERVAL
        asked_since = None
        while time.monotonic() < deadline:
            self.stop_if_cancelled()
            try:
                self.captcha()
                if time.monotonic() >= next_ask:
                    next_ask = time.monotonic() + LIST_ASK_INTERVAL
                    if self._accounts_listed():
                        return False
                if watch_mfa and self.mfa_requested():
                    asked_since = time.monotonic() if asked_since is None else asked_since
                    if time.monotonic() - asked_since >= self.mfa_settle:
                        # A page can look like a check while it is signed in, so the list decides.
                        return not self._accounts_listed()
                else:
                    asked_since = None
            except JavascriptException:
                pass  # the page navigated while it was being read
            time.sleep(SIGN_IN_POLL)
        raise NeedsUserAction(f"{self.display_name} did not finish signing in; sync again")

    def _accounts_listed(self) -> bool:
        """Ask the bank for the account list. An answer means signed in, and its list is kept for
        the downloads. A refusal, a failed request, or anything else means not yet."""
        try:
            self._listed = self.site_accounts()
        except SyncCancelled:
            raise
        except (ConnectorError, JavascriptException):
            return False
        return True

    def _await_approval(self) -> None:
        """Show the bank's QR or push prompt and wait until the person approves in the bank's app,
        which signs the page in. The QR is shown again whenever the bank replaces it. A screen that
        stays gone without a sign-in means the approval was declined or expired."""
        prompt = self._the_prompt()
        deadline = time.monotonic() + APPROVAL_TIMEOUT
        next_ask = time.monotonic() + LIST_ASK_INTERVAL
        shown: str | None = None
        announced_push = False
        gone_since: float | None = None
        with timed(self.key, "approval"):
            try:
                while time.monotonic() < deadline:
                    self.stop_if_cancelled()
                    if time.monotonic() >= next_ask:
                        next_ask = time.monotonic() + LIST_ASK_INTERVAL
                        if self._accounts_listed():
                            return
                    try:
                        image = self.approval_image()
                        waiting = image is not None or self.approval_waiting()
                    except JavascriptException:
                        image = shown  # the page navigated while it was being read
                        waiting = True
                    if not waiting:
                        gone_since = time.monotonic() if gone_since is None else gone_since
                        if time.monotonic() - gone_since >= APPROVAL_GONE:
                            raise NeedsUserAction(
                                f"{self.display_name} did not sign in after the approval "
                                "screen closed; sync again"
                            )
                    elif image is not None:
                        gone_since = None
                        if image != shown:
                            prompt.show(
                                f"Scan the QR with your phone, then approve in the "
                                f"{self.display_name} app.",
                                image=image,
                            )
                            shown = image
                    else:
                        gone_since = None
                        if not announced_push:
                            prompt.show(f"Approve the sign-in in the {self.display_name} app.")
                            announced_push = True
                    time.sleep(SIGN_IN_POLL)
            finally:
                prompt.show("")
        raise NeedsUserAction(f"no approval came in time for {self.display_name}; sync again")

    def stop_if_cancelled(self) -> None:
        """Raise SyncCancelled when the person cancelled the sync."""
        if self._prompt is not None and self._prompt.cancelled():
            raise SyncCancelled

    def _the_prompt(self) -> Prompt:
        if self._prompt is None:
            raise RuntimeError("fetch() must set the prompt first")
        return self._prompt

    def _ask_code(self) -> str:
        """Ask for the code the bank sent. A timeout or an empty answer needs the person."""
        try:
            answer = self._the_prompt().ask(
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

    # Hooks. The defaults suit a bank with no captcha and no MFA. The first two are asked many
    # times a second, so they only read the page.

    def mfa_requested(self) -> bool:
        """Hook: True when the bank is asking how to send a code."""
        return False

    def captcha(self) -> None:
        """Hook for banks that show a CAPTCHA. Raises NeedsUserAction when one is shown."""

    def login(self) -> None:
        """Automates login using configured credentials and web elements."""
        self.driver.get(self._sign_in_url)
        self.wait_for_ajax_load()
        credentials = self._credentials()
        self.send_keys_to_element((By.ID, self.username_id), credentials.username or "")
        self.send_keys_to_element((By.ID, self.password_id), credentials.password)
        self.click_element((By.ID, self.login_id))

    def request_auth(self) -> Check:
        """Hook: ask the bank to check it is you, and say how it will: a code it sends, or an
        approval in its app, whose QR approval_image reads."""
        return Check.code

    def approval_image(self) -> str | None:
        """Hook: the QR the bank shows for an approval in its app, as a data:image URL, or None
        when the page does not show one."""
        return None

    def approval_waiting(self) -> bool:
        """Hook: True while the bank's app-approval screen is still shown (for push flows without
        a QR image)."""
        return False

    def authenticate(self, code: str) -> None:
        """Hook: type the MFA code and continue."""

    def site_accounts(self) -> list[SiteAccount]:
        """Hook: the accounts the bank lists. It is also the sign-in signal: it returns only when
        the bank answered with its list, and raises NeedsUserAction or ConnectorError otherwise,
        such as when the bank refuses a session that is not signed in yet."""
        raise NotImplementedError

    def entries_for(self, last4: str, site_accounts: list[SiteAccount]) -> list[SiteAccount]:
        """Every entry with these last four digits, which is more than one when a bank lists an
        account twice. All are fetched: import joins rows that repeat and keeps any that differ."""
        entries = [entry for entry in site_accounts if entry.last4 == last4]
        if not entries:
            raise self.account_not_shown(last4)
        return entries

    def prepare_downloads(self) -> Any:
        """Hook: what every download needs, such as a token. It is passed to fetch_statements and
        not stored, because a registered connector serves repeated syncs."""
        return None

    def fetch_statements(
        self, site_account: SiteAccount, account: ConnectorAccount, prepared: Any
    ) -> list[str]:
        """Hook: what the bank answered for one entry, one text per statement."""
        raise NotImplementedError

    def is_statement(self, text: str) -> bool:
        """Hook: whether an answer is a statement. A failed request can have a success status."""
        return "<OFX>" in text.upper()

    def failure_reason(self, text: str) -> str:
        """Hook: why an answer was not a statement, as far as it is safe to show."""
        return NOT_A_STATEMENT if text.strip() else "the bank returned an empty response"

    def statement_folder(self, account: ConnectorAccount) -> str:
        """Hook: the subfolder for an account's files."""
        return folder_for(account)

    def get_account_activity(self) -> None:
        """Fetch and keep every configured account's statements. An account the site does not list
        ends the fetch before anything is requested."""
        if not self._accounts:
            return
        site_accounts = self._listed if self._listed is not None else self.site_accounts()
        wanted = [
            (account, self.entries_for(site_last4(account), site_accounts))
            for account in self._accounts
        ]
        prepared = self.prepare_downloads()
        fetched: set[str] = set()
        for account, entries in wanted:
            self.stop_if_cancelled()
            new_entries = [entry for entry in entries if entry.key not in fetched]
            if new_entries:
                fetched.update(entry.key for entry in new_entries)
                self._keep_statements(account, new_entries, prepared)

    def _keep_statements(
        self, account: ConnectorAccount, entries: list[SiteAccount], prepared: Any
    ) -> None:
        last4 = site_last4(account)
        texts = [
            text for entry in entries for text in self.fetch_statements(entry, account, prepared)
        ]
        if not texts:
            self.skip_account(last4, "the bank listed no statements")
        for number, text in enumerate(texts, start=1):
            if self.is_statement(text):
                self.keep_text(
                    text,
                    stem=self.stem_for(last4, number if len(texts) > 1 else None),
                    suffix=self.statement_suffix,
                    subfolder=self.statement_folder(account),
                )
            else:
                self.skip_account(last4, self.failure_reason(text))

    # Keeping files.

    def skip_account(self, last4: str, reason: str) -> None:
        """Record that the bank gave nothing usable for one account. The other accounts are still
        fetched and kept, and the sync reports this one as failed."""
        self._skipped.append(f"{self.display_name} gave nothing for ••{last4}: {reason}")

    def keep_text(self, text: str, *, stem: str, suffix: str, subfolder: str | None = None) -> None:
        """Keep text a page returned, such as an XHR response, as a file."""
        self._check_subfolder(subfolder)
        path = self.scratch_dir() / f"response-{len(self._downloads)}{suffix}"
        path.write_text(text, encoding="utf-8")
        self._downloads.append(Download(path, stem, suffix, subfolder))

    def stem_for(self, last4: str, number: int | None = None) -> str:
        """A file name with the last four digits and the window, never a full account number."""
        stem = f"{self.key}_{last4}_{self._period.start:%Y%m%d}_to_{self._period.end:%Y%m%d}"
        return stem if number is None else f"{stem}_{number}"

    def account_not_shown(self, last4: str) -> ConnectorError:
        return ConnectorError(
            f"{self.display_name} did not show the account ••{last4}. If its card was reissued, "
            "add the new card's last four under Numbers on the account's page"
        )

    @staticmethod
    def _check_subfolder(subfolder: str | None) -> None:
        if subfolder is not None and not FOLDER_NAME.fullmatch(subfolder):
            raise ConnectorError("a statement account ID cannot name a folder")

    def _store(self, dest: Path) -> list[Path]:
        """Move each kept file into dest/<account>/<year>, under a name that is never taken."""
        stored: list[Path] = []
        year = str(self._period.start.year)
        for item in self._downloads:
            account_subfolder = item.subfolder or "default"
            folder = dest / account_subfolder / year
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
