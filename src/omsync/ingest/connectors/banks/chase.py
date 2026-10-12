"""Chase personal and business statement acquisition."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from omsync.ingest.connectors.banks.base import BankConnector, site_last4
from omsync.ingest.connectors.base import ConnectorAccount
from omsync.ingest.connectors.registry import register

DDA_ACCOUNT_KEYWORDS = ["CHECKING", "SAVINGS", "CHK", "SAV"]
DOWNLOAD_PAGE = (
    "https://secure.chase.com/web/auth/dashboard"
    "#/dashboard/accountDetails/downloadAccountTransactions/index"
)
DDA_DOWNLOAD_URL = (
    "https://secure.chase.com/svc/rr/accounts/secure/v1/account/activity/download/dda/list"
)
CARD_DOWNLOAD_URL = (
    "https://secure.chase.com/svc/rr/accounts/secure/gateway/credit-card/transactions/"
    "inquiry-maintenance/digital-transaction-activity/v1/transaction-activities?"
)
# Headers a page script may set and a replayed request repeats. Cookies, origin, and the like are
# the browser's to send.
_REPLAYED_HEADER = re.compile(r"x-.*|accept|content-type", re.IGNORECASE)


@dataclass(frozen=True)
class SiteRequest:
    """A download request the website itself sent, to repeat for other accounts and dates."""

    url: str
    headers: dict[str, str]
    body: str = ""

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


class _ChaseConnector(BankConnector):
    """Statement download behavior shared by the explicit personal and business connectors."""

    code_digits = 8
    fallback_sign_in_url = "https://secure.chase.com/web/auth/#/logon/logon/chaseOnline"
    username_id = "userId-text-input-field"
    password_id = "password-text-input-field"
    login_id = "signin-button"
    frame = ""
    sign_in_kind = ""
    card_eligibility_required = False

    def __init__(self) -> None:
        super().__init__()
        self._csrftoken = ""
        self._site_requests: dict[str, SiteRequest] = {}

    def _is_dda(self, account: dict[str, Any]) -> bool:
        return _kind_of(account) == "dda"

    def get_csrftoken(self, account: dict[str, Any]) -> None:
        # Currently, csrftoken cannot be found from scraping html nor cookies
        # It can only be found in the url of a GET Request of account download
        # Therefore, we will perform a manual download of any account
        # Note that csrftoken only appears based on the following condition(s):
        # 1. The GET Request must return at least one transaction. The date range constructed in
        #    the request must capture at least one transaction.
        # To ensure that we have data, we temporarily overwrite the window to last year.
        # The assumption is that there is at least 1 transaction in a year or else the account
        # would have been closed without any activity
        # A registered connector instance serves repeated syncs, so discard the earlier run's state.
        self._csrftoken = ""
        self._site_requests = {}
        fromdate = self.fromdate
        todate = self._todate
        today = datetime.now()
        self.fromdate = today - timedelta(days=365)
        self.todate = today
        # The download only makes the site send the request. Its file is discarded.
        self.download_ofx(account, manual=True, save=False)
        # The site's own request is kept whole, so the downloads go to the host it used, with its
        # headers and parameters, rather than to a request rebuilt here.
        for log in self.get_logs(["Network.requestWillBeSent"]):
            request = log.get("message", {}).get("params", {}).get("request", {})
            url, body = request.get("url", ""), request.get("postData", "")
            # A checking download posts the token in its body. A card download sends it in the URL.
            match = re.search("csrftoken=([^&]+)", f"{body}&{url}")
            if not match:
                continue
            self._csrftoken = self._csrftoken or match[1]
            kind = _download_kind(url)
            if kind is None:
                continue
            headers = {
                key: value
                for key, value in request.get("headers", {}).items()
                if _REPLAYED_HEADER.fullmatch(key)
            }
            self._site_requests.setdefault(kind, SiteRequest(url, headers, body))
        self.fromdate = fromdate
        self.todate = todate

    def _asked(self, kind: str) -> str:
        """The connector flow, requested dates, token status, and request source for a safe failure
        message. The token itself is never shown."""
        window = f"{self.fromdate:%Y%m%d} to {self.todate:%Y%m%d}"
        token = "captured" if self._csrftoken else "missing"
        site = self._site_requests.get(kind)
        source = (
            "built by omsync"
            if site is None
            else f"repeated the site's request to {site.host} from {self.base_url()}"
        )
        return (
            f" ({self.sign_in_kind} sign-in; asked for {window}; security token {token}; {source})"
        )

    def close_popup(self, timeout: float = 3) -> None:
        try:
            close = self.click_element(
                (By.CSS_SELECTOR, "button.button--secondary.button--fuild"), timeout=timeout
            )
            self.wait_for_page_load(close)
        except (NoSuchElementException, TimeoutException):
            pass

    def get_account_activity(self) -> None:
        bank_accounts = self.wait().until(EC.element_to_be_clickable((By.ID, "DDA_ACCOUNTS")))
        self.driver.get(DOWNLOAD_PAGE)
        self.wait_for_page_load(bank_accounts)
        site_accounts = self.get_account()
        matches = [
            (valid, self._site_account_for(site_accounts, valid)) for valid in self._accounts
        ]
        if not matches:
            return
        self.get_csrftoken(matches[0][1])
        for valid, site_account in matches:
            self.download_ofx(site_account, last4=site_last4(valid))

    def _site_account_for(self, site_accounts: list[dict[str, Any]], account: ConnectorAccount):
        last4 = site_last4(account)
        for site_account in site_accounts:
            if last4 in site_account["name"]:
                return site_account
        raise self.account_not_shown(last4)

    def get_account(self) -> list[dict[str, Any]]:
        account_selector = self.wait().until(
            EC.element_to_be_clickable((By.ID, "account-selector"))
        )
        return json.loads(account_selector.get_attribute("options"))

    def download_ofx(
        self,
        account: dict[str, Any],
        downloadType: str = "QFX",
        manual: bool = False,
        save: bool = True,
        *,
        last4: str = "",
    ) -> None:
        if not manual:
            format_time = "%Y%m%d"
            kind = _kind_of(account)
            site = self._site_requests.get(kind)
            if kind == "dda":
                data = {
                    "dateHi": self.todate.strftime(format_time),
                    "dateLo": self.fromdate.strftime(format_time),
                    "statementPeriodId": "ALL",
                    "transactionType": "ALL",
                    "filterTranType": "ALL",
                    "downloadType": downloadType,
                    "accountId": account["value"],
                    "csrftoken": self._csrftoken,
                    "submit": "Submit",
                }
                if site is None:
                    result_ofx = self.xhr_post_urlencoded(DDA_DOWNLOAD_URL, data)
                else:
                    result_ofx = self.xhr_request(
                        "POST", site.url, _with_fields(site.body, data), site.headers
                    )
            else:
                # CREDIT CARD account transaction name is always incomplete regardless of file
                # format. The OFX is kept as it is.
                data = {
                    "end-date": self.todate.strftime(format_time),
                    "start-date": self.fromdate.strftime(format_time),
                    "account-activity-download-type-code": downloadType,
                    "digital-account-identifier": account["value"],
                    "csrftoken": self._csrftoken,
                    "submit": "Submit",
                }
                if self.card_eligibility_required:
                    data["eligibility-indicator"] = "true"
                if site is None:
                    query = "&".join(f"{key}={value}" for key, value in data.items())
                    result_ofx = self.xhr_get_url(CARD_DOWNLOAD_URL + query)
                else:
                    parts = urlsplit(site.url)
                    url = urlunsplit(parts._replace(query=_with_fields(parts.query, data)))
                    result_ofx = self.xhr_request("GET", url, None, site.headers)
            if not result_ofx:
                self.skip_account(last4, f"the bank returned an empty response{self._asked(kind)}")
            elif "<OFX>" not in result_ofx:
                # A failed request can answer with a short JSON error and a success status.
                self.skip_account(last4, f"{_bank_error(result_ofx)}{self._asked(kind)}")
            else:
                self.keep_text(result_ofx, stem=self.stem_for(last4), suffix=".ofx")
        else:
            # The elements can be updated with each value change, so we must do one at a time.
            self.set_element_attribute((By.ID, "account-selector"), "value", account["value"])
            self.set_element_attribute((By.ID, "downloadFileTypeOption"), "value", downloadType)
            # Click time period to reveal date elements
            timeperiod_dropdown = self.click_element((By.ID, "downloadActivityOptionId"))
            timeperiod_items = timeperiod_dropdown.find_elements(By.TAG_NAME, "mds-select-option")
            timeperiod_items[1 if self._is_dda(account) else -1].click()
            format_time = "%m/%d/%Y"
            self.send_keys_to_element(
                (By.ID, "accountActivityFromDate"),
                self.fromdate.strftime(format_time),  # Get the start of the year
            )
            self.send_keys_to_element(
                (By.ID, "accountActivityToDate"),
                self.todate.strftime(format_time),
            )
            # After sending keys to todate field, must click anywhere to accept the value.
            timeperiod_dropdown.click()
            timeperiod_dropdown.click()
            # Complete download
            download = self.download_file(lambda: self.click_element((By.ID, "download")))
            if save:
                self.keep_file(
                    self.require_download(download), stem=self.stem_for(last4), suffix=".ofx"
                )
            elif download is not None:
                # Only a request for the csrftoken: the file is not kept.
                download.unlink()
            # Navigate back to download page
            self.click_element((By.ID, "downloadOtherActivity"))


@register
class ChaseConnector(_ChaseConnector):
    """Chase personal login and verification flow."""

    key = "chase"
    label = "Chase: OFX statements from the website (personal login)"
    display_name = "Chase"
    sign_in_kind = "personal"
    url_auth = "confirmIdentity"

    def request_auth(self, timeout: float = 5) -> bool:
        try:
            self.wait(timeout).until(EC.url_contains(self.url_auth))
            options, _items = self.get_element_items((By.ID, "optionsList"))
            self.click_element(options)
            self.click_element((By.ID, "next-content"))
            return True
        except Exception:
            return False

    def authenticate(self, code: str) -> None:
        try:
            self.send_keys_to_element((By.ID, "otpInput"), str(code))
            next_button = self.click_element((By.ID, "next-content"))
            self.wait_for_page_load(next_button)
        except Exception:
            pass


@register
class ChaseBusinessConnector(_ChaseConnector):
    """Chase Business login, verification, and card download requirements."""

    key = "chase_business"
    label = "Chase Business: OFX statements from the website (business login)"
    display_name = "Chase Business"
    sign_in_kind = "business"
    url_auth = "simplerAuthOptions"
    card_eligibility_required = True

    def request_auth(self, timeout: float = 5) -> bool:
        try:
            self.wait(timeout).until(EC.url_contains(self.url_auth))
            self.click_element((By.ID, "header-simplerAuth-dropdownoptions-styledselect"))
            self.click_element(
                (By.ID, "container-primary-1-simplerAuth-dropdownoptions-styledselect")
            )
            self.click_element((By.ID, "requestIdentificationCode-sm"))
            return True
        except Exception:
            return False

    def authenticate(self, code: str) -> None:
        try:
            self.send_keys_to_element((By.ID, "otpcode_input-input-field"), str(code))
            self.send_keys_to_element(
                (By.ID, "password_input-input-field"), self._credentials().password
            )
            next_button = self.click_element((By.ID, "log_on_to_landing_page-sm"))
            self.wait_for_page_load(next_button)
        except Exception:
            pass


def _kind_of(account: dict[str, Any]) -> str:
    """ "dda" for a checking or savings account, otherwise "card", as its download is requested."""
    return (
        "dda" if any(word in account["name"].upper() for word in DDA_ACCOUNT_KEYWORDS) else "card"
    )


def _download_kind(url: str) -> str | None:
    """Which activity download a request is, card or dda, whatever its host or API version. None
    for any other request."""
    path = urlsplit(url).path
    if "digital-transaction-activity" in path:
        return "card"
    if "/activity/download/dda/" in path:
        return "dda"
    return None


def _with_fields(encoded: str, fields: dict[str, str]) -> str:
    """The site's form or query string with the fields that pick the account and dates replaced.
    Its own csrftoken and any field omsync does not know are kept as the site sent them."""
    merged = dict(parse_qsl(encoded, keep_blank_values=True))
    merged.update({key: value for key, value in fields.items() if key != "csrftoken"})
    return urlencode(merged)


def _bank_error(response: str) -> str:
    """What a failed request answered, as far as it is safe to show: the code and the description
    of Chase's JSON error. Anything else is not repeated."""
    try:
        body = json.loads(response)
        return (
            f"the bank answered with error {body['serviceErrorCode']}: {body['errorDescription']}"
        )
    except (ValueError, KeyError, TypeError):
        return "the bank answered with something other than a statement"
