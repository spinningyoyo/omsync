"""Bank of America statement acquisition."""

import html
import json
import re
from urllib.parse import urlencode

from selenium.webdriver.common.by import By

from omsync.ingest.connectors.banks.base import BankConnector, Check, SiteAccount, folder_for
from omsync.ingest.connectors.base import ConnectorAccount, NeedsUserAction
from omsync.ingest.connectors.registry import register

ACCOUNTS_DATA = "/ogateway/ps-dashboard/online/accounts-overview/v1?request_locale=en-us"
# The accounts page sends its list request with no body and no token, only these headers.
ACCOUNTS_HEADERS = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
DEPOSIT_DOWNLOAD = "/ogateway/addapi/v1/download/form/transaction"
CARD_PAGE = "/myaccounts/brain/redirect.go"
LAST4_IN_NAME = re.compile(r"(\d{4})\s*$")  # a display name ends "... - 1234"


@register
class BoaConnector(BankConnector):
    key = "boa"
    # Its CSV folders are named by the last four digits, so those are the statement account IDs.
    last4_is_statement_id = True
    label = "Bank of America: CSV statements from the website"
    display_name = "Bank of America"
    fallback_sign_in_url = "https://secure.bankofamerica.com/login/sign-in/signOnV2Screen.go"
    username_id = "oid"
    password_id = "pass"
    login_id = "secure-signin-submit"
    url_security = "signOnRedirect"
    statement_suffix = ".csv"

    def mfa_requested(self) -> bool:
        return self.page_has("#authcodeReceiveType")

    def captcha(self) -> None:
        if self.url_security in self.driver.current_url:
            raise NeedsUserAction(
                f"{self.display_name} asked for a CAPTCHA, which omsync cannot complete; "
                "sign in to the bank directly, then sync again."
            )

    def request_auth(self) -> Check:
        # Send text to authentication device
        self.click_element(
            (
                By.CSS_SELECTOR,
                "#authcodeReceiveType > div.spa-input-options-list."
                "spa-input-options-list--vertical > div:nth-child(1) > label",
            )
        )
        self.click_element((By.ID, "ah-authcode-select-continue-btn"))
        return Check.code

    def authenticate(self, code: str) -> None:
        # Complete auth
        self.send_keys_to_element((By.ID, "ahAuthcodeValidateOTP"), code)
        self.click_element(
            (
                By.CSS_SELECTOR,
                "#authcodeSecPref > div.spa-input-options-list."
                "spa-input-options-list--vertical > div:nth-child(1) > label",
            )
        )
        self.click_element((By.ID, "ah-authcode-validate-continue-btn"))

    def site_accounts(self) -> list[SiteAccount]:
        """Each account with its adx, the key of its download requests, from the list the accounts
        page asks for, requested directly."""
        answer = self.xhr_request(
            "POST", f"{self.base_url()}{ACCOUNTS_DATA}", None, ACCOUNTS_HEADERS
        )
        try:
            categories = json.loads(answer)["accounts-overview"]["productCategories"]
        except (ValueError, KeyError, TypeError):
            raise NeedsUserAction(
                f"{self.display_name} did not list its accounts; sync again"
            ) from None
        found: list[SiteAccount] = []
        for category in categories:
            for brand in category.get("brandCategories", []):
                for custom in brand.get("customCategories", []):
                    for account in custom.get("accounts", []):
                        # The identifier's number fields are masked. Only the name has the digits.
                        name = LAST4_IN_NAME.search(account.get("displayName", ""))
                        if name:
                            found.append(SiteAccount(name[1], account["identifier"]["adx"]))
        return found

    def fetch_statements(
        self, site_account: SiteAccount, account: ConnectorAccount, prepared: None
    ) -> list[str]:
        if account.type == "credit_card":
            return self._card_statements(site_account.key)
        return [self._deposit_activity(site_account.key)]

    def _deposit_activity(self, adx: str) -> str:
        format_time = "%m/%d/%Y"
        data = {
            "payload.accountToken": adx,
            "payload.locale": "en-us",
            "payload.txnSearchCriteria.txnPeriod": "custom range",
            "payload.txnSearchCriteria.startDate": self.fromdate.strftime(format_time),
            "payload.txnSearchCriteria.endDate": self.todate.strftime(format_time),
            "payload.txnSearchCriteria.fileType": "csv",
        }
        return self.xhr_request(
            "POST",
            f"{self.base_url()}{DEPOSIT_DOWNLOAD}",
            urlencode(data),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )

    def _card_statements(self, adx: str) -> list[str]:
        # Credit card account transactions are available only by statement date. A statement with
        # nothing in it answers empty, which is not a failure.
        query = urlencode({"target": "acctDetails", "adx": adx})
        page = self.xhr_request("GET", f"{self.base_url()}{CARD_PAGE}?{query}")
        answers = [
            self.xhr_request("GET", f"{self.base_url()}{link}") for link in _statement_links(page)
        ]
        return [answer for answer in answers if answer]

    def is_statement(self, text: str) -> bool:
        """False for an empty answer or an HTML page, which is how a failed request answers."""
        return bool(text.strip()) and not text.lstrip().startswith("<")

    def statement_folder(self, account: ConnectorAccount) -> str:
        return folder_for(account)


def _statement_links(page: str) -> list[str]:
    """The download link of each statement in a card page's dropdown. An option's value is an
    account-details link, and the download link is the same path with two parts replaced. This
    breaks if the site changes its link format."""
    dropdown = re.search(r'id="goto_select_trans_top".*?</select>', page, re.DOTALL)
    if dropdown is None:
        return []
    links = []
    for value in re.findall(r'<option[^>]*value="([^"]*)"', dropdown[0]):
        link = html.unescape(value)
        if "/account-details.go?" not in link:
            continue
        link = link.replace("/account-details.go?", "/download-transactions.go?")
        link = link.replace("&target=stmtFromDateList", "&target=downloadStmtFromDateList")
        links.append(f"{link}&formatType=csv")
    return links
