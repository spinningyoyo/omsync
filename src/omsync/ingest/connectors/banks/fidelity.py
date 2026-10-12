"""Fidelity Rewards statement acquisition."""

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from omsync.ingest.connectors.banks.base import BankConnector, site_last4
from omsync.ingest.connectors.registry import register


@register
class FidelityConnector(BankConnector):
    key = "fidelity"
    label = "Fidelity Rewards: QFX statements from the website"
    display_name = "Fidelity"
    fallback_sign_in_url = "https://login.fidelityrewards.com/"
    username_id = "input_aw-personal-id"
    password_id = "input_aw-password"
    login_id = "login-button-continue"
    frame = ""
    url_auth = "login.fidelityrewards.com"

    def request_auth(self, timeout: float = 5) -> bool:
        # Send text to authentication device
        try:
            self.wait(timeout).until(EC.url_contains(self.url_auth))
            self.click_element((By.ID, "otp-cont-button"))
            return True
        except Exception:
            return False

    def authenticate(self, code: str) -> None:
        # Complete auth
        try:
            self.send_keys_to_element((By.ID, "input_idshield-input"), code)
            login_button = self.click_element((By.ID, "otp-cont-button"))
            self.wait_for_page_load(login_button)
        except Exception:
            pass

    def close_popup(self, timeout: float = 3) -> None:
        try:
            close = self.click_element((By.ID, "cancelBtn"), timeout=timeout)
            self.wait_for_page_load(close)
        except (NoSuchElementException, TimeoutException):
            pass

    def get_account_activity(self) -> None:
        # There is only one account, so one download covers it. If more accounts are configured,
        # they share this file. Its ACCTID says which account it holds.
        if not self._accounts:
            return
        self.download_qfx(site_last4(self._accounts[0]))

    def download_qfx(self, last4: str) -> None:
        # Click download button to bring up the download page
        self.click_element((By.ID, "transaction-download-button"))
        # Find elements in the download modal page
        _, filetype_items = self.get_element_items(
            (By.ID, "select_download-types"), (By.TAG_NAME, "option")
        )
        ofx_item = filetype_items[2]  # Index 2 is Qfx
        # Complete the download
        format_time = "%m/%d/%Y"
        self.send_keys_to_element(
            (By.NAME, "startDate"),
            self.fromdate.strftime(format_time),  # Get the start of the year
            check=False,  # skip the check because date gets reformatted after keys are sent
        )
        self.send_keys_to_element(
            (By.NAME, "endDate"),
            self.todate.strftime(format_time),
            check=False,  # skip the check because date gets reformatted after keys are sent
        )
        ofx_item.click()
        qfx_path = self.require_download(
            self.download_file(
                lambda: self.click_element((By.ID, "transactionsDownloadOptionsDownload"))
            )
        )
        self.keep_file(qfx_path, stem=self.stem_for(last4), suffix=".ofx")
        self.click_element((By.ID, "transactionsDownloadOptionsDismiss"))
