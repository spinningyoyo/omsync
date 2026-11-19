"""Citi statement acquisition."""

import json

from selenium.webdriver.common.by import By

from omsync.ingest.connectors.banks.base import BankConnector, Check, SiteAccount
from omsync.ingest.connectors.base import ConnectorAccount, ConnectorError, NeedsUserAction
from omsync.ingest.connectors.registry import register

ACCOUNTS_URL = "https://online.citi.com/gcgapi/prod/public/v1/v2/digital/customers/dashboardTiles/accountDetails"


@register
class CitiConnector(BankConnector):
    key = "citi"
    label = "Citi: OFX statement"
    display_name = "Citi"
    fallback_sign_in_url = "https://online.citi.com/US/login.do"
    username_id = "username"
    password_id = "password"
    login_id = "signInBtn"
    # The code request is an ordinary page with a primary button, and so may the page before the
    # account links load.
    mfa_settle = 3

    def mfa_requested(self) -> bool:
        return not self.page_has(f"#{self.username_id}") and self.page_has("button.btn.btn-primary")

    def request_auth(self) -> Check:
        # Send text to authentication device.
        self.click_element((By.CSS_SELECTOR, "button.btn.btn-primary"))
        return Check.code

    def authenticate(self, code: str) -> None:
        # Complete auth. A second button does not always follow, and the sign-in watch decides.
        self.send_keys_to_element((By.ID, "otpToken"), code)
        self.click_element((By.CSS_SELECTOR, "button.btn.btn-primary"))
        self.click_element((By.CSS_SELECTOR, "button.btn.btn-primary"))

    def site_accounts(self) -> list[SiteAccount]:
        """Each card with its accountId, from the list the dashboard asks for, requested
        directly. displayAccountNumber holds the last four digits."""
        answer = self.xhr_request("GET", ACCOUNTS_URL, None, _api_headers(self._client_id()))
        try:
            cards = json.loads(answer)["creditCardAccount"]["accountDetails"]
            return [
                SiteAccount(str(card["displayAccountNumber"]), str(card["accountId"]))
                for card in cards
            ]
        except (ValueError, KeyError, TypeError):
            raise NeedsUserAction(
                f"{self.display_name} did not list its accounts; sync again"
            ) from None

    def prepare_downloads(self) -> str:
        return self._client_id()

    def _client_id(self) -> str:
        """The client_id cookie, which every API request repeats as a header."""
        client_cookie = self.driver.get_cookie("client_id")
        if client_cookie is None:
            raise ConnectorError(f"{self.display_name} did not set the session cookie omsync needs")
        return client_cookie["value"]

    def fetch_statements(
        self, site_account: SiteAccount, account: ConnectorAccount, prepared: str
    ) -> list[str]:
        format_time = "%m/%d/%Y"
        url = (
            "https://online.citi.com/gcgapi/prod/public/v1/cbol/accounts/"
            f"{site_account.key}/transactions/download"
        )
        data = {
            "timePeriodFilter": {
                "filterIndicator": "DATE_RANGE",
                "groupFilterIndicator": "CUSTOM",
                "startDate": self.fromdate.strftime(format_time),
                "endDate": self.todate.strftime(format_time),
                "displayValue": "Date range",
            },
            "searchKeyword": None,
            "creditCardTransactionType": None,
            "selectedCardId": None,
            "fileFormat": "OFX",
            "moreTransactionsFlag": "false",
        }
        return [self.xhr_request("POST", url, json.dumps(data), _api_headers(prepared))]


def _api_headers(client_id: str) -> dict[str, str]:
    """The headers the API needs. The site sends more, such as channelid, and the API answers the
    same without them."""
    return {
        "Content-Type": "application/json",
        "Client_id": client_id,
        "Accept": "application/json",
    }
