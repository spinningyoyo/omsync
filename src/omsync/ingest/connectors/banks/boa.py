"""Bank of America statement acquisition."""

import re

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from omsync.ingest.connectors.banks.base import BankConnector, folder_for, site_last4
from omsync.ingest.connectors.base import ConnectorAccount, NeedsUserAction
from omsync.ingest.connectors.registry import register


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
    frame = ""
    url_auth = "auth"
    url_security = "signOnRedirect"

    def url_overview(self):
        return re.search(r"myaccounts", self.driver.current_url, re.IGNORECASE)

    def captcha(self, timeout: float = 3) -> None:
        try:
            self.wait(timeout).until(EC.url_contains(self.url_security))
        except TimeoutException:
            return
        raise NeedsUserAction(
            f"{self.display_name} asked for a CAPTCHA, which omsync cannot complete; "
            "sign in to the bank directly, then sync again."
        )

    def request_auth(self, timeout: float = 5) -> bool:
        # Send text to authentication device
        try:
            self.wait(timeout).until(EC.url_contains(self.url_auth))
            self.click_element(
                (
                    By.CSS_SELECTOR,
                    "#authcodeReceiveType > div.spa-input-options-list."
                    "spa-input-options-list--vertical > div:nth-child(1) > label",
                )
            )
            self.click_element((By.ID, "ah-authcode-select-continue-btn"))
            return True
        except Exception:
            return False

    def authenticate(self, code: str) -> None:
        # Complete auth
        try:
            self.send_keys_to_element((By.ID, "ahAuthcodeValidateOTP"), code)
            self.click_element(
                (
                    By.CSS_SELECTOR,
                    "#authcodeSecPref > div.spa-input-options-list."
                    "spa-input-options-list--vertical > div:nth-child(1) > label",
                )
            )
            login_button = self.click_element((By.ID, "ah-authcode-validate-continue-btn"))
            self.wait_for_page_load(login_button)
        except Exception:
            pass

    def close_popup(self, timeout: float = 3) -> None:
        close_buttons = [
            (By.CSS_SELECTOR, "button.spa-card__dismissible"),
            (By.ID, "btn-not-now"),
        ]
        for close_button in close_buttons:
            try:
                close = self.click_element(close_button, timeout=timeout)
                self.wait_for_page_load(close)
                break
            except (NoSuchElementException, TimeoutException):
                pass

    def get_account_name_from_element(self, account) -> str:
        # The located element is the account's own anchor, so its title and text are read directly.
        if self.url_overview():
            return account.get_attribute("title") or ""
        return (account.get_attribute("textContent") or "").strip().split("\n")[0]

    def get_account_activity(self) -> None:
        # BOA has two types of pages: overview and individual account
        # where element names/classes/ids are different.
        for account_valid in self._accounts:
            account_number = site_last4(account_valid)
            account_tab, accounts = self.get_account()
            for account_found in accounts:
                if account_number in self.get_account_name_from_element(account_found):
                    if not self.url_overview():
                        # Click tab to reveal the hidden account links
                        account_tab.click()
                    account_found.click()
                    self.wait_for_ajax_load()
                    self.download_statements(account_valid)
                    break
            else:
                raise self.account_not_shown(account_number)

    def get_account(self):
        if self.url_overview():
            # Move the cursor to the account tab to reveal the hidden account links
            def reveal_items(tab) -> None:
                ActionChains(self.driver).move_to_element(tab).perform()

            account_tab, accounts = self.get_element_items(
                (By.ID, "fsd-li-accounts"),
                (By.CSS_SELECTOR, "li#fsd-li-accounts a.fsd_accounts_link"),
                reveal_items,
            )
        else:
            account_tab = self.wait().until(EC.element_to_be_clickable((By.ID, "nav-accounts-L1")))
            accounts = self.driver.find_elements(
                By.CSS_SELECTOR,
                "div.account-listings-module a.account-item.link",
            )
        return account_tab, accounts

    def download_statements(self, account: ConnectorAccount, filetype: str = "csv") -> None:
        last4 = site_last4(account)
        if self.url_overview():
            # Credit card account transactions are available only by statement date. The links
            # for all available statements are in the selection, so each link is requested.
            for number, url in enumerate(self._get_download_links(filetype), start=1):
                text = self.xhr_get_url(url)
                if text:
                    self.keep_text(
                        text,
                        stem=self.stem_for(last4, number),
                        suffix=".csv",
                        subfolder=folder_for(account),
                    )
        else:
            # Checking or saving account transactions can be custom date.
            # Click download button to bring up the download page
            self.click_element((By.ID, "download-transactions"))
            # Find elements in the download modal page
            _, timeperiod_items = self.get_element_items(
                (By.ID, "select_txnPeriod"), (By.TAG_NAME, "option")
            )
            item_custom_range = timeperiod_items[1]  # Index 1 is Custom Range
            _, filetype_items = self.get_element_items(
                (By.ID, "select_fileType"), (By.TAG_NAME, "option")
            )
            csv_item = filetype_items[1]
            # Complete the download
            item_custom_range.click()
            format_time = "%m/%d/%Y"
            self.send_keys_to_element(
                (By.ID, "datepicker_fromDate"),
                self.fromdate.strftime(format_time),  # Get the start of the year
            )
            self.send_keys_to_element(
                (By.ID, "datepicker_toDate"),
                self.todate.strftime(format_time),
            )
            csv_item.click()
            csv_path = self.require_download(
                self.download_file(lambda: self.click_element((By.ID, "btn-download-txn")))
            )
            self.keep_file(
                csv_path,
                stem=self.stem_for(last4),
                suffix=".csv",
                subfolder=folder_for(account),
            )
            self.click_element((By.CSS_SELECTOR, "button.spa-card__dismissible"))

    def _get_download_links(self, filetype: str = "qfx") -> list[str]:
        # The statement dropdown on the overview page holds one option per statement. Each value
        # is an account-details link, and the download link is the same path with two parts
        # replaced. This breaks if the site changes its link format.
        timeperiod_dropdown = self.wait().until(
            EC.presence_of_element_located((By.ID, "goto_select_trans_top"))
        )
        links: list[str] = []
        for option in timeperiod_dropdown.find_elements(By.TAG_NAME, "option"):
            link = (option.get_attribute("value") or "").replace(
                "/account-details.go?", "/download-transactions.go?"
            )
            link = link.replace("&target=stmtFromDateList", "&target=downloadStmtFromDateList")
            links.append(f"{self.base_url()}{link}&formatType={filetype}")
        return links
