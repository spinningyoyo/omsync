"""Walmart order and receipt acquisition through an authenticated Chrome session."""

import json
import logging
import subprocess
import time
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from selenium.common.exceptions import JavascriptException, WebDriverException

from omsync.core.settings import Settings
from omsync.core.vault import Vault
from omsync.ingest.connectors.base import ConnectorError, NeedsUserAction, Prompt, SyncCancelled
from omsync.receipts.connectors.base import VendorConnector
from omsync.receipts.connectors.registry import register
from omsync.receipts.vendors import get_vendor

logger = logging.getLogger(__name__)

ORDERS_URL = "https://www.walmart.com/orders"
PURCHASE_HISTORY_URL = (
    "https://www.walmart.com/orchestra/cph/graphql/PurchaseHistoryV3/"
    "d42c97aea418eab500018785dc64e688fb4311a800fd1babc29176ac6c39a1bd"
)
GET_ORDER_URL = (
    "https://www.walmart.com/orchestra/orders/graphql/getOrder/"
    "8716adf016d3fcdc3ed3b26e3724819ea3c6831bc5a5a9782fc4816126221e6c"
)

SIGN_IN_QUESTION = (
    "Sign in to Walmart in the browser window (including any verification code), "
    "then select I have signed in."
)
SIGN_IN_TIMED_OUT = "no confirmation came in time for the Walmart sign-in; sync again"
SIGN_IN_NOT_DONE = "Walmart sign-in was not completed in the browser window; sign in and sync again"

GET_JSON_SCRIPT = """
const url = arguments[0];
const headers = arguments[1];
const callback = arguments[arguments.length - 1];

fetch(url, {
    method: "GET",
    headers: headers,
    credentials: "include"
})
.then(r => {
    if (!r.ok) return callback({ ok: false, status: r.status });
    return r.json().then(data => callback({ ok: true, data: data, status: r.status }));
})
.catch(err => callback({ ok: false, error: String(err) }));
"""


def _parse_order_date(raw_date: Any) -> date:
    if not raw_date:
        return date.today()
    try:
        return datetime.fromisoformat(str(raw_date).strip()).date()
    except ValueError:
        return date.today()


def _write_json(target: Path, receipt: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    temporary.replace(target)


@register
class WalmartConnector(VendorConnector):
    """Walmart order and purchase acquisition connector."""

    key = "walmart"
    label = "Walmart"
    uses_vault = False

    def download_receipts(
        self,
        driver: Any,
        settings: Settings,
        prompt: Prompt,
        *,
        provider_key: str,
        held: Mapping[str, Path | None],
        start: date,
        end: date,
        vault: Vault | None = None,
        vault_item_id: str | None = None,
    ) -> list[Path]:
        """Sign in and download the provider's Walmart orders and receipts."""
        driver.get(ORDERS_URL)
        if not self.signed_in(driver):
            driver = self._sign_in_with_clean_chrome(prompt)
        self.ensure_orders_page(driver)
        return self.download_new(
            driver, settings.receipts_dir, provider_key, held, start=start, end=end
        )

    def _sign_in_with_clean_chrome(self, prompt: Prompt) -> Any:
        """Sign in via an un-debugged Chrome so PerimeterX and Akamai allow authentication."""
        self.close()
        proc = self.launch_clean_browser(ORDERS_URL)

        try:
            try:
                confirmed = prompt.confirm(SIGN_IN_QUESTION, button="I have signed in")
            except (TimeoutError, SyncCancelled):
                raise NeedsUserAction(SIGN_IN_TIMED_OUT) from None
            if not confirmed:
                raise NeedsUserAction(SIGN_IN_TIMED_OUT)
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        driver = self.get_driver(self.browser_mode)
        driver.get(ORDERS_URL)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.signed_in(driver):
                break
            time.sleep(0.5)
        if not self.signed_in(driver):
            raise NeedsUserAction(SIGN_IN_NOT_DONE)

        return driver

    def signed_in(self, driver: Any) -> bool:
        """Whether the browser session has valid Walmart authentication cookies."""
        try:
            url = driver.current_url or ""
            if "identity.walmart.com" in url or "/account/login" in url or "verifyToken" in url:
                return False
            auth_cookie = driver.get_cookie("auth")
            customer_cookie = driver.get_cookie("customer")
            return bool(
                (auth_cookie and auth_cookie.get("value"))
                or (customer_cookie and customer_cookie.get("value"))
            )
        except (WebDriverException, JavascriptException):
            return False

    def ensure_orders_page(self, driver: Any) -> None:
        """Navigate to the orders page if not already there."""
        url = driver.current_url or ""
        if "walmart.com/orders" not in url:
            driver.get(ORDERS_URL)

    def download_new(
        self,
        driver: Any,
        receipts_dir: Path,
        provider_key: str,
        held: Mapping[str, Path | None],
        *,
        start: date,
        end: date,
    ) -> list[Path]:
        """Page through orders from newest to oldest, saving those in start..end."""
        vendor = get_vendor(self.key)
        cursor = ""
        written: list[Path] = []
        reached_before_start = False

        while not reached_before_start:
            page_data = self._fetch_orders_page(driver, cursor)
            ph = page_data.get("data", {}).get("purchaseHistory", {})
            orders = ph.get("orders") or []
            if not orders:
                break

            for order in orders:
                order_id = str(order.get("id") or "").strip()
                if not order_id:
                    continue

                order_date = _parse_order_date(order.get("orderDate"))
                if order_date < start:
                    reached_before_start = True
                    break
                if order_date > end:
                    continue

                if order_id in held:
                    continue

                is_in_store = bool(order.get("isInStore"))
                detail_data = self._fetch_order_detail(driver, order_id, is_in_store)
                target = vendor.receipt_file(receipts_dir, provider_key, order_date, order_id)
                _write_json(target, detail_data)
                written.append(target)

            page_info = ph.get("pageInfo") or {}
            next_cursor = page_info.get("nextPageCursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        return written

    def _fetch_orders_page(self, driver: Any, cursor: str) -> dict[str, Any]:
        variables = {
            "input": {
                "cursor": cursor,
                "search": "",
                "filterIds": [],
                "limit": 10,
                "type": None,
                "minTimestamp": None,
                "maxTimestamp": None,
                "filters": {"minTimestamp": None, "maxTimestamp": None, "filterIds": []},
                "enabledFeatures": [],
                "isStrictSearch": False,
                "eligibleFeatures": {
                    "isEbtEligible": False,
                    "enablePhFiltersEnhancement": True,
                    "isBnbEligible": False,
                    "isWcpAccEligible": False,
                    "isLotOnOdpEnable": True,
                    "isSolutranEligible": False,
                    "enable3pReshop": True,
                    "enableTeenAccounts": False,
                    "enableAgenticBanner": True,
                },
                "searchItemIdentifierId": "",
            },
            "platform": "WEB",
            "enableIsWcpOrder": False,
            "enableWcpPhaseOrder": False,
            "enableReturnStatusTracker": False,
        }
        url = (
            PURCHASE_HISTORY_URL
            + "?"
            + urlencode({"variables": json.dumps(variables, separators=(",", ":"))})
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-o-gql-query": "query PurchaseHistoryV3",
            "X-APOLLO-OPERATION-NAME": "PurchaseHistoryV3",
            "x-o-platform": "rweb",
            "x-o-mart": "B2C",
            "x-o-segment": "oaoh",
            "WM_MP": "true",
        }
        result = driver.execute_async_script(GET_JSON_SCRIPT, url, headers)
        if not result or not result.get("ok"):
            status = result.get("status") if result else "unknown"
            raise ConnectorError(f"failed to fetch Walmart purchase history (status {status})")
        return result.get("data") or {}

    def _fetch_order_detail(self, driver: Any, order_id: str, is_in_store: bool) -> dict[str, Any]:
        variables = {
            "orderId": order_id,
            "orderIsInStore": is_in_store,
            "clickThroughGroupId": "0",
            "enableIsWcpOrder": False,
            "enableWcpPhaseOrder": False,
            "enableUniquePaymentIdentifier": False,
            "enabledFeatures": ["csc", "csat-northstar-v1"],
            "enableSignOnDelivery": True,
            "enableVolumePricing": False,
            "enableCancelFix": False,
            "enableGroupBannerMessages": True,
            "eligibleFeatures": {
                "isEbtEligible": False,
                "isLotOnOdpEnable": True,
                "isLmdReturnsBarcodeDisabled": True,
                "isSolutranEligible": False,
                "enableReportSeller": False,
                "enable3pReshop": True,
                "enableTeenAccounts": False,
                "enableAgenticBanner": True,
            },
        }
        url = (
            GET_ORDER_URL
            + "?"
            + urlencode({"variables": json.dumps(variables, separators=(",", ":"))})
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-o-gql-query": "query getOrder",
            "X-APOLLO-OPERATION-NAME": "getOrder",
            "x-o-platform": "rweb",
            "x-o-mart": "B2C",
            "x-o-segment": "oaoh",
            "WM_MP": "true",
        }
        result = driver.execute_async_script(GET_JSON_SCRIPT, url, headers)
        if not result or not result.get("ok"):
            status = result.get("status") if result else "unknown"
            raise ConnectorError(f"failed to fetch Walmart order {order_id} (status {status})")
        return result.get("data") or {}
