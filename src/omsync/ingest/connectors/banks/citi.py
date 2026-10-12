"""Citi statement acquisition."""

import json
import re

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from omsync.ingest.connectors.banks.base import BankConnector, site_last4
from omsync.ingest.connectors.base import ConnectorError
from omsync.ingest.connectors.registry import register


@register
class CitiConnector(BankConnector):
    key = "citi"
    label = "Citi: OFX statements from the website"
    display_name = "Citi"
    fallback_sign_in_url = "https://online.citi.com/US/login.do"
    username_id = "username"
    password_id = "password"
    login_id = "signInBtn"
    frame = ""
    url_auth = "citi.com"

    def close_popup(self, timeout: float = 3) -> None:
        try:
            # Deals and Destinations modal
            close = self.click_element((By.ID, "secondaryCTA"), timeout=timeout)
            self.wait_for_page_load(close)
        except (NoSuchElementException, TimeoutException):
            pass

    def request_auth(self, timeout: float = 5) -> bool:
        # Send text to authentication device.
        # url_auth matches every Citi page; the button click distinguishes a code request.
        try:
            self.wait(timeout).until(EC.url_contains(self.url_auth))
            self.click_element((By.CSS_SELECTOR, "button.btn.btn-primary"))
            return True
        except Exception:
            return False

    def authenticate(self, code: str, timeout: float = 3) -> None:
        # Complete auth
        try:
            self.send_keys_to_element((By.ID, "otpToken"), code)
            self.click_element((By.CSS_SELECTOR, "button.btn.btn-primary"))
            continue_button = self.click_element((By.CSS_SELECTOR, "button.btn.btn-primary"))
            self.wait_for_page_load(continue_button)
        except Exception:
            pass

    def get_account_activity(self) -> None:
        accounts = self.get_account()
        for account_valid in self._accounts:
            # Citi's pages show the last four digits, so the match is on those.
            last4 = site_last4(account_valid)
            for account_id, account_last4 in accounts:
                if last4 == account_last4:
                    self.download_ofx(account_id, last4=last4)
                    break
            else:
                raise self.account_not_shown(last4)

    def get_account(self) -> list[tuple[str, str]]:
        """The (account ID, last four) of each account the navigation links to. The last four are
        the end of the link's text, such as "Costco Any...-1234"."""
        accounts = []
        for link in self.driver.find_elements(
            By.CSS_SELECTOR, "citi-navigation3 a[href*=accountId]"
        ):
            match = re.search("accountId=([^&]+)", link.get_attribute("href") or "", re.IGNORECASE)
            if match:
                text = (link.get_attribute("textContent") or "").strip()
                accounts.append((match[1], text[-4:]))
        return accounts

    def download_ofx(self, account: str, downloadType: str = "OFX", *, last4: str = "") -> None:
        # Find elements in the account page
        format_time = "%m/%d/%Y"
        url = (
            "https://online.citi.com/gcgapi/prod/public/v1/cbol/accounts/"
            f"{account}/transactions/download"
        )
        data = {
            "timePeriodFilter": {
                "filterIndicator": "DATE_RANGE",
                "groupFilterIndicator": "CUSTOM",
                "startDate": self.fromdate.strftime(format_time),
                "endDate": self.todate.strftime(format_time),
                "displayValue": "Date range",
            },
            "searchKeyword": "null",
            "creditCardTransactionType": "null",
            "selectedCardId": "null",
            "fileFormat": f"{downloadType}",
            "moreTransactionsFlag": "false",
        }
        client_cookie = self.driver.get_cookie("client_id")
        if client_cookie is None:
            raise ConnectorError(f"{self.display_name} did not set the session cookie omsync needs")
        headers = {
            "Content-Type": "application/json",
            "Client_id": f"{client_cookie['value']}",
            "Accept": "application/json",
        }
        # After converting json to str, remove "" around null and add escape character for quote (")
        result = self.xhr_post_urlencoded(
            url, json.dumps(data).replace('"null"', "null").replace('"', '\\"'), headers
        )
        if result:
            self.keep_text(result, stem=self.stem_for(last4), suffix=".ofx")
