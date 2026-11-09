"""Chase personal and business statement acquisition."""

import json
import re
import time
import uuid
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlencode

from selenium.common.exceptions import JavascriptException
from selenium.webdriver.common.by import By

from omsync.ingest.connectors.banks.base import (
    NOT_A_STATEMENT,
    SIGN_IN_POLL,
    BankConnector,
    Check,
    SiteAccount,
)
from omsync.ingest.connectors.base import ConnectorAccount, NeedsUserAction
from omsync.ingest.connectors.registry import register

TOKEN_URL = "https://secure.chase.com/svc/rl/accounts/secure/v1/csrf/token/list"
ACCOUNTS_URL = (
    "https://secure.chase.com/svc/rr/accounts/secure/v1/account/activity/download/options/list"
)
DDA_DOWNLOAD_URL = (
    "https://secure.chase.com/svc/rr/accounts/secure/v1/account/activity/download/dda/list"
)
CARD_DOWNLOAD_URL = (
    "https://secure.chase.com/svc/rr/accounts/secure/gateway/credit-card/transactions/"
    "inquiry-maintenance/digital-transaction-activity/v1/transaction-activities"
)
FORM_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"
ACCEPT = "application/json, text/plain, */*"
# The identity check, personal and Business. Its screens share one step in the address and differ
# in its caas= part. See docs/architecture/bank-requests/chase.md, "Sign-in check".
IDENTITY_STEP = "confirmIdentity"
OLDER_BUSINESS_STEP = "simplerAuthOptions"  # Business's screen before 2026-11
CHOOSE_SCREEN = "options"
APP_SCREEN = "inAppPull"
QR_SCREEN = "inAppPullVerify"
SCREEN_WAIT = 20  # seconds for a screen of the identity check to show
OPTIONS_SCRIPT = """
const list = document.querySelector('#optionsList');
if (!list) return [];
const root = list.shadowRoot || list;
return [...root.querySelectorAll('li')].map(item => (item.innerText || item.textContent).trim());
"""
# The control of one entry: its link, the chevron at the row's right. The entry's label comes
# before it and is only text, so a click on the label does nothing.
OPTION_SCRIPT = """
const list = document.querySelector('#optionsList');
const item = (list.shadowRoot || list).querySelectorAll('li')[arguments[0]];
return item.querySelector('a.list-item__navigational') || item.querySelector('a, input, button')
    || item;
"""
QR_SCRIPT = """
const image = document.querySelector('#qrCodeImage');
return image ? image.getAttribute('src') : null;
"""
T = TypeVar("T")


class _ChaseConnector(BankConnector):
    """Statement download behavior shared by the explicit personal and business connectors."""

    code_digits = 8
    fallback_sign_in_url = "https://secure.chase.com/web/auth/#/logon/logon/chaseOnline"
    username_id = "userId-text-input-field"
    password_id = "password-text-input-field"
    login_id = "signin-button"
    sign_in_kind = ""

    def _asked(self) -> str:
        """The connector flow and requested dates, for a safe failure message."""
        window = f"{self.fromdate:%Y%m%d} to {self.todate:%Y%m%d}"
        return f" ({self.sign_in_kind} sign-in; asked for {window})"

    def mfa_requested(self) -> bool:
        return IDENTITY_STEP in self.driver.current_url

    def _answer_field(self, url: str, field: str, missing: str) -> Any:
        """One field of an API answer. A session that went wrong answers with something else."""
        answer = self.xhr_request("POST", url, None, _api_headers(FORM_TYPE))
        try:
            return json.loads(answer)[field]
        except (ValueError, KeyError, TypeError):
            raise NeedsUserAction(f"{self.display_name} did not {missing}; sync again") from None

    def site_accounts(self) -> list[SiteAccount]:
        """The accounts Chase offers downloads for, the list behind its download page. Chase
        refuses it with 401 until sign-in, the identity check included, is complete."""
        options = self._answer_field(
            ACCOUNTS_URL, "downloadAccountActivityOptions", "list its accounts"
        )
        return [
            SiteAccount(
                str(entry.get("mask")), str(entry["accountId"]), entry.get("summaryType", "")
            )
            for entry in options
        ]

    def prepare_downloads(self) -> str:
        return self._answer_field(TOKEN_URL, "csrfToken", "give a security token")

    def fetch_statements(
        self, site_account: SiteAccount, account: ConnectorAccount, prepared: str
    ) -> list[str]:
        format_time = "%Y%m%d"
        if site_account.kind == "DDA":
            data = {
                "dateHi": self.todate.strftime(format_time),
                "dateLo": self.fromdate.strftime(format_time),
                "statementPeriodId": "ALL",
                "transactionType": "ALL",
                "filterTranType": "ALL",
                "downloadType": "QFX",
                "accountId": site_account.key,
                "csrftoken": prepared,
                "submit": "Submit",
            }
            headers = {"Accept": ACCEPT, "Content-Type": FORM_TYPE}
            return [self.xhr_request("POST", DDA_DOWNLOAD_URL, urlencode(data), headers)]
        # CREDIT CARD account transaction name is always incomplete regardless of file
        # format. The OFX is kept as it is.
        data = {
            "end-date": self.todate.strftime(format_time),
            "start-date": self.fromdate.strftime(format_time),
            "account-activity-download-type-code": "QFX",
            "digital-account-identifier": site_account.key,
            "csrftoken": prepared,
            "submit": "Submit",
            "eligibility-indicator": "true",
        }
        url = f"{CARD_DOWNLOAD_URL}?{urlencode(data)}"
        return [self.xhr_request("GET", url, None, _api_headers("application/json; charset=UTF-8"))]

    def failure_reason(self, text: str) -> str:
        reason = _bank_error(text) if text.strip() else super().failure_reason(text)
        return f"{reason}{self._asked()}"

    # The identity check, the same for personal and Business since 2026-11.

    def request_auth(self) -> Check:
        """Choose how Chase checks it is you: a code when Chase offers one, otherwise an approval
        in the Chase app. Each step is known by its screen, so a skipped choice does not matter."""
        screen = self._screen()
        if screen == CHOOSE_SCREEN:
            labels = self._until(self._option_labels, "list the ways to confirm it's you")
            index = _preferred_option(labels)
            if index is None:
                raise NeedsUserAction(
                    "Chase offered no way to confirm it's you that a sync can use; sign in at "
                    "Chase directly, then sync again"
                )
            # A real click, as a person makes, on the element the script finds in the list.
            self.click_element(self.driver.execute_script(OPTION_SCRIPT, index))
            if not _is_app(labels[index]):
                return self._send_code()
            screen = self._until(self._screen_after_choice, "open the app approval")
        if screen == APP_SCREEN:
            self.click_element((By.ID, "next-content"))
            self._until(lambda: self._screen() == QR_SCREEN, "show the QR for the app approval")
            return Check.approval
        if screen == QR_SCREEN:
            return Check.approval
        return self._send_code()

    def approval_image(self) -> str | None:
        if self._screen() != QR_SCREEN:
            return None
        source = self.driver.execute_script(QR_SCRIPT)
        return source if isinstance(source, str) and source.startswith("data:image/") else None

    def authenticate(self, code: str) -> None:
        self.send_keys_to_element((By.ID, "otpInput"), str(code))
        self.click_element((By.ID, "next-content"))

    def _send_code(self) -> Check:
        """Ask Chase to send the code the chosen way, with Next, as before the app approval."""
        self.click_element((By.ID, "next-content"))
        return Check.code

    def _screen(self) -> str:
        """The caas= part of the address, which names the identity check's screen."""
        match = re.search(r"[;?&]caas=([^;&#]+)", self.driver.current_url)
        return match.group(1) if match else ""

    def _screen_after_choice(self) -> str | None:
        screen = self._screen()
        return None if screen in ("", CHOOSE_SCREEN) else screen

    def _option_labels(self) -> list[str] | None:
        labels = self.driver.execute_script(OPTIONS_SCRIPT)
        return [str(label) for label in labels] if labels else None

    def _until(self, read: Callable[[], T | None], what: str) -> T:
        """What read returns once it is something, looking a few times a second. Chase not
        showing it within SCREEN_WAIT ends the sync as needing the person."""
        deadline = time.monotonic() + SCREEN_WAIT
        while time.monotonic() < deadline:
            self.stop_if_cancelled()
            try:
                value = read()
            except JavascriptException:
                value = None  # the page navigated while it was being read
            if value:
                return value
            time.sleep(SIGN_IN_POLL)
        raise NeedsUserAction(f"{self.display_name} did not {what}; sync again")


@register
class ChaseConnector(_ChaseConnector):
    """Chase personal login."""

    key = "chase"
    label = "Chase: OFX from the website"
    display_name = "Chase"
    sign_in_kind = "personal"


@register
class ChaseBusinessConnector(_ChaseConnector):
    """Chase Business login. Its identity check is personal's, and the older Business screen,
    which also asked for the password again, is still followed if Chase shows it."""

    key = "chase_business"
    label = "Chase Business: OFX from the website"
    display_name = "Chase Business"
    sign_in_kind = "business"

    def __init__(self) -> None:
        super().__init__()
        self._older_screen = False

    def mfa_requested(self) -> bool:
        return super().mfa_requested() or OLDER_BUSINESS_STEP in self.driver.current_url

    def request_auth(self) -> Check:
        self._older_screen = OLDER_BUSINESS_STEP in self.driver.current_url
        if not self._older_screen:
            return super().request_auth()
        self.click_element((By.ID, "header-simplerAuth-dropdownoptions-styledselect"))
        self.click_element((By.ID, "container-primary-1-simplerAuth-dropdownoptions-styledselect"))
        self.click_element((By.ID, "requestIdentificationCode-sm"))
        return Check.code

    def authenticate(self, code: str) -> None:
        if not self._older_screen:
            super().authenticate(code)
            return
        self.send_keys_to_element((By.ID, "otpcode_input-input-field"), str(code))
        self.send_keys_to_element(
            (By.ID, "password_input-input-field"), self._credentials().password
        )
        self.click_element((By.ID, "log_on_to_landing_page-sm"))


def _is_app(label: str) -> bool:
    return "mobile app" in label.lower()


def _preferred_option(labels: list[str]) -> int | None:
    """The way to confirm it's you that a sync takes: the first that sends a code, otherwise the
    app approval. Calling Chase is never chosen, since only a person can do it."""
    usable = [i for i, label in enumerate(labels) if "call us" not in label.lower()]
    codes = [i for i in usable if not _is_app(labels[i])]
    apps = [i for i in usable if _is_app(labels[i])]
    return (codes or apps or [None])[0]


def _api_headers(content_type: str) -> dict[str, str]:
    """The headers the site's scripts set on an API request. The CSRF header's value is literal."""
    return {
        "Accept": ACCEPT,
        "Content-Type": content_type,
        "x-jpmc-csrf-token": "NONE",
        "x-jpmc-channel": "id=C30",
        "x-jpmc-client-request-id": str(uuid.uuid4()),
    }


def _bank_error(response: str) -> str:
    """What a failed request answered, as far as it is safe to show: the code and the description
    of Chase's JSON error. Anything else is not repeated."""
    try:
        body = json.loads(response)
        return (
            f"the bank answered with error {body['serviceErrorCode']}: {body['errorDescription']}"
        )
    except (ValueError, KeyError, TypeError):
        return NOT_A_STATEMENT
