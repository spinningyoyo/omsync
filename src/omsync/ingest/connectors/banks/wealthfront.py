"""Wealthfront statement acquisition."""

import json
import urllib.parse

from selenium.webdriver.common.by import By

from omsync.core.vault import LoginCredentials
from omsync.ingest.connectors.banks.base import BankConnector, Check, SiteAccount
from omsync.ingest.connectors.base import ConnectorAccount, ConnectorError, NeedsUserAction
from omsync.ingest.connectors.registry import register

OVERVIEWS_URL = "/capitan/v1/accounts/overviews2"
ACCOUNT_IDS_URL = "/api/wealthfront_accounts/get_account_ids_for_user"
ACCOUNT_DETAILS_URL = "/api/wealthfront_accounts/{internal_id}/details"
DEBIT_STATUS_URL = "/capitan/v1/accounts/{internal_id}/debit/status"
FILTERS_URL = "/capitan/v1/accounts/{account_id}/cash-account-transaction-filters"
DOWNLOAD_URL = "/documents/{account_id}/cash-activity/{filter_id}"


@register
class WealthfrontConnector(BankConnector):
    key = "wealthfront"
    label = "Wealthfront: CSV statement"
    display_name = "Wealthfront"
    fallback_sign_in_url = "https://www.wealthfront.com/login"
    username_id = "login-username"
    password_id = "login-password"
    last4_is_statement_id = True
    statement_suffix = ".csv"

    def _resolve_sign_in_url(self, login: LoginCredentials) -> str:
        url = super()._resolve_sign_in_url(login)
        parts = urllib.parse.urlsplit(url)
        if parts.path.rstrip("/") in ("", "/"):
            return urllib.parse.urljoin(url, "/login")
        return url

    def login(self) -> None:
        target_url = self._sign_in_url
        parts = urllib.parse.urlsplit(target_url)
        if parts.path.rstrip("/") in ("", "/"):
            target_url = urllib.parse.urljoin(target_url, "/login")
        self.driver.get(target_url)
        self.wait_for_ajax_load()
        if "/login" not in self.driver.current_url:
            self.driver.get(self.fallback_sign_in_url)
            self.wait_for_ajax_load()
        credentials = self._credentials()
        self.send_keys_to_element((By.ID, self.username_id), credentials.username or "")
        self.send_keys_to_element((By.ID, self.password_id), credentials.password)
        self.click_element(
            (
                By.CSS_SELECTOR,
                "button[data-testid='pingdom-login-button'], form button[type='submit']",
            )
        )

    def mfa_requested(self) -> bool:
        return "/mfa" in self.driver.current_url or self.page_has("#mfa-auth-code")

    def request_auth(self) -> Check:
        return Check.code

    def authenticate(self, code: str) -> None:
        self.send_keys_to_element((By.ID, "mfa-auth-code"), code)
        self.click_element((By.CSS_SELECTOR, "form button[type='submit']"))

    def _base_url(self) -> str:
        base = self.base_url()
        return base if base.startswith("http") else "https://www.wealthfront.com"

    def _api_headers(self) -> dict[str, str]:
        headers = {"Accept": "*/*", "WF-App-Id": "wf-web"}
        cookie = self.driver.get_cookie("xsrf")
        if cookie:
            headers["X-Csrf-Token"] = urllib.parse.unquote(cookie["value"])
        return headers

    def site_accounts(self) -> list[SiteAccount]:
        """Ask Wealthfront for the account list. An answer confirms the session is signed in."""
        answer = self.xhr_request(
            "GET", f"{self._base_url()}{OVERVIEWS_URL}", None, self._api_headers()
        )
        try:
            overviews = json.loads(answer)["overviews"]
        except (ValueError, KeyError, TypeError):
            raise NeedsUserAction(
                f"{self.display_name} did not list its accounts; sync again"
            ) from None

        cash_accounts = [
            acc
            for acc in overviews
            if acc.get("type") == "cash-account" or "CASH" in acc.get("accountType", "")
        ]
        if not cash_accounts:
            raise NeedsUserAction(f"{self.display_name} did not list any cash accounts; sync again")

        external_ids = [acc["accountId"] for acc in cash_accounts if "accountId" in acc]
        internal_ids = self._internal_account_ids(external_ids)

        accounts: list[SiteAccount] = []
        for acc in cash_accounts:
            ext_id = acc.get("accountId")
            if not ext_id:
                continue
            registered: set[str] = set()
            for candidate in self._account_candidates(ext_id, internal_ids.get(ext_id)):
                last4 = candidate[-4:]
                if last4 not in registered:
                    registered.add(last4)
                    accounts.append(SiteAccount(last4, ext_id))

        if not accounts:
            raise NeedsUserAction(f"{self.display_name} did not list any accounts; sync again")

        return accounts

    def _account_candidates(self, ext_id: str, int_id: int | None) -> list[str]:
        checking_number = self._checking_account_number(ext_id)
        if not checking_number and int_id:
            checking_number = self._checking_account_number(int_id)
        acct_number = self._account_number(int_id) if int_id else ""
        return [c for c in (checking_number, acct_number, ext_id) if len(c) >= 4]

    def _internal_account_ids(self, external_ids: list[str]) -> dict[str, int]:
        if not external_ids:
            return {}
        query = urllib.parse.quote(",".join(external_ids))
        url = f"{self._base_url()}{ACCOUNT_IDS_URL}?accountIds={query}"
        mapping_resp = self.xhr_request("GET", url, None, self._api_headers())
        try:
            return json.loads(mapping_resp).get("data", {})
        except (ValueError, KeyError, TypeError):
            return {}

    def _account_number(self, internal_id: int) -> str:
        details_url = f"{self._base_url()}{ACCOUNT_DETAILS_URL.format(internal_id=internal_id)}"
        details_resp = self.xhr_request("GET", details_url, None, self._api_headers())
        try:
            return json.loads(details_resp).get("accountName", "")
        except (ValueError, KeyError, TypeError):
            return ""

    def _checking_account_number(self, account_id: str | int) -> str:
        url = f"{self._base_url()}{DEBIT_STATUS_URL.format(internal_id=account_id)}"
        resp = self.xhr_request("GET", url, None, self._api_headers())
        try:
            data = json.loads(resp)
            if not isinstance(data, dict):
                return ""
            for section in ("accountRoutingInfo", "directDepositInfo"):
                section_data = data.get(section)
                if isinstance(section_data, dict):
                    identifiers = section_data.get("identifiers")
                    if isinstance(identifiers, dict):
                        num = identifiers.get("accountNumber")
                        if num and str(num).isdigit() and len(str(num)) >= 4:
                            return str(num)
            status_data = data.get("status")
            if isinstance(status_data, dict):
                num = status_data.get("accountNumber")
                if num and str(num).isdigit() and len(str(num)) >= 4:
                    return str(num)
            return ""
        except (ValueError, KeyError, TypeError, AttributeError):
            return ""

    def entries_for(self, last4: str, site_accounts: list[SiteAccount]) -> list[SiteAccount]:
        unique_keys = {sa.key for sa in site_accounts}
        if len(unique_keys) == 1:
            return [sa for sa in site_accounts if sa.key in unique_keys]
        return super().entries_for(last4, site_accounts)

    def prepare_downloads(self) -> dict[str, str]:
        cookie = self.driver.get_cookie("xsrf")
        if cookie is None:
            raise ConnectorError(f"{self.display_name} did not set the session cookie omsync needs")
        return {
            "X-Csrf-Token": urllib.parse.unquote(cookie["value"]),
            "WF-App-Id": "wf-web",
            "Accept": "text/csv, text/plain, */*",
        }

    def fetch_statements(
        self, site_account: SiteAccount, account: ConnectorAccount, prepared: dict[str, str]
    ) -> list[str]:
        account_id = site_account.key
        filters_url = f"{self._base_url()}{FILTERS_URL.format(account_id=account_id)}"
        filters_resp = self.xhr_request("GET", filters_url, None, prepared)
        available_years: set[str] = set()
        try:
            filters_data = json.loads(filters_resp)
            year_ranges = filters_data.get("downloadTimeRanges", {}).get("yearTimeRanges", [])
            available_years = {item["id"] for item in year_ranges if "id" in item}
        except (ValueError, KeyError, TypeError):
            available_years = set()

        wanted_years = [str(y) for y in range(self.fromdate.year, self.todate.year + 1)]
        target_filters = [y for y in wanted_years if y in available_years]

        if not target_filters:
            target_filters = ["all"]

        statements: list[str] = []
        for filter_id in target_filters:
            path = DOWNLOAD_URL.format(account_id=account_id, filter_id=filter_id)
            url = f"{self._base_url()}{path}"
            resp = self.xhr_request("GET", url, None, prepared)
            if resp:
                statements.append(resp)
        return statements

    def is_statement(self, text: str) -> bool:
        return "Transaction date,Description,Type,Amount" in text
