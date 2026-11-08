"""Fidelity Rewards statement acquisition."""

import json
import uuid
from datetime import date, timedelta

from selenium.webdriver.common.by import By

from omsync.ingest.connectors.banks.base import BankConnector, Check, SiteAccount
from omsync.ingest.connectors.base import ConnectorAccount, NeedsUserAction
from omsync.ingest.connectors.registry import register

API_PATH = "/digital/api/partner-services/graphql/v1"
# The site sends a download with an end date of today as four days later.
TODAY_END_OFFSET = timedelta(days=4)
# Session storage the signed-in page sets: the bearer token, the accounts, and the partner details
# the site's own code puts in its API headers. The list counts as answered only when all are there.
SIGNED_IN_KEYS = ("AccessToken", "multiAccountMetaData", "PartnerConfig")


@register
class FidelityConnector(BankConnector):
    key = "fidelity"
    label = "Fidelity Rewards: QFX statements from the website"
    display_name = "Fidelity"
    fallback_sign_in_url = "https://login.fidelityrewards.com/"
    username_id = "input_aw-personal-id"
    password_id = "input_aw-password"
    login_id = "login-button-continue"

    def mfa_requested(self) -> bool:
        return self.page_has("#otp-cont-button")

    def request_auth(self) -> Check:
        # Send text to authentication device
        self.click_element((By.ID, "otp-cont-button"))
        return Check.code

    def authenticate(self, code: str) -> None:
        # Complete auth
        self.send_keys_to_element((By.ID, "input_idshield-input"), code)
        self.click_element((By.ID, "otp-cont-button"))

    def _stored(self, key: str) -> str | None:
        return self.driver.execute_script("return sessionStorage.getItem(arguments[0])", key)

    def prepare_downloads(self) -> dict[str, str]:
        """The partner headers, built as the site's own code builds them: three from the partner
        details in session storage, and two the site sends as fixed values."""
        try:
            partner = json.loads(self._stored("PartnerConfig") or "")["partnerInfo"]
            return {
                "customerPartnerId": str(partner["partnerID"]),
                "customerGroupId": str(partner["groupID"]),
                "customerPartnerLoc": str(partner["loc"]),
                "Service-Version": "2",
                "routingKey": "",
            }
        except (ValueError, KeyError, TypeError):
            raise NeedsUserAction(
                f"{self.display_name} did not show the details a download needs; sync again"
            ) from None

    def site_accounts(self) -> list[SiteAccount]:
        """The accounts the signed-in page stored. Fidelity's page keeps its list in session
        storage rather than requesting it, so the list counts as answered once the page has stored
        the token, the accounts and the partner details."""
        if not all(self._stored(key) for key in SIGNED_IN_KEYS):
            raise NeedsUserAction(f"{self.display_name} did not list its accounts; sync again")
        try:
            stored = json.loads(self._stored("multiAccountMetaData") or "")
            return [
                SiteAccount(entry.get("details", {}).get("cardLast4PAN", ""), entry["accountToken"])
                for entry in stored["accounts"]
            ]
        except (ValueError, KeyError, TypeError):
            raise NeedsUserAction(
                f"{self.display_name} did not list its accounts; sync again"
            ) from None

    def entries_for(self, last4: str, site_accounts: list[SiteAccount]) -> list[SiteAccount]:
        # A lone account is the one wanted even after a reissue. The file's ACCTID identifies it.
        if len(site_accounts) == 1:
            return site_accounts
        return super().entries_for(last4, site_accounts)

    def fetch_statements(
        self, site_account: SiteAccount, account: ConnectorAccount, prepared: dict[str, str]
    ) -> list[str]:
        end = self.todate.date()
        if end == date.today():
            end += TODAY_END_OFFSET
        body = {
            "requestType": {
                "serviceType": "ACCOUNT_TRANSACTION",
                "serviceSubType": "HISTORY_DOWNLOAD",
            },
            "data": {
                "accountToken": site_account.key,
                "searchBy": [],
                "startTime": self.fromdate.strftime("%Y-%m-%d"),
                "endTime": end.strftime("%Y-%m-%d"),
                "fileType": "QFX",
            },
        }
        headers = {
            **prepared,
            "Authorization": f"Bearer {self._stored('AccessToken')}",
            "Content-Type": "application/json",
            "Correlation-ID": str(uuid.uuid4()),
            "application-id": "RPCTRANDOWNLOADCRDTXN",
        }
        url = f"{self.base_url()}{API_PATH}/downloads"
        return [self.xhr_request("POST", url, json.dumps(body), headers)]
