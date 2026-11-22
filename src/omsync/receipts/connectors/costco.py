"""Costco warehouse receipt acquisition through an authenticated Chrome session."""

import json
import subprocess
import time
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from selenium.common.exceptions import JavascriptException, WebDriverException

from omsync.core.months import shift_months
from omsync.core.settings import Settings
from omsync.core.vault import Vault
from omsync.ingest.connectors.base import ConnectorError, NeedsUserAction, Prompt, SyncCancelled
from omsync.receipts.connectors.base import VendorConnector
from omsync.receipts.connectors.registry import register
from omsync.receipts.vendors import get_vendor
from omsync.receipts.vendors.costco import receipt_date

ACCOUNT_HOME_URL = "https://www.costco.com/myaccount/"
GRAPHQL_URL = "https://ecom-api.costco.com/ebusiness/order/v1/orders/graphql"
CLIENT_IDENTIFIER = "481b1aec-aa3b-454b-b81b-48187e28f205"  # hardcoded in the site's web app
GRAPHQL_TIMEOUT = 120  # seconds, for the browser request and the script timeout
CHUNK_MONTHS = 3

SIGN_IN_QUESTION = (
    "Sign in to Costco in the browser window (including any verification code), "
    "then select I have signed in."
)
SIGN_IN_NOT_DONE = "Costco sign-in was not completed in the browser window; sign in and sync again"
SIGN_IN_TIMED_OUT = "no confirmation came in time for the Costco sign-in; sync again"
SESSION_MISSING = "Costco did not return a session; sign in again, then sync"

RECEIPTS_QUERY = """
query receiptsWithCounts(
  $startDate: String!,
  $endDate: String!,
  $documentType: String!,
  $documentSubType: String!
) {
  receiptsWithCounts(
    startDate: $startDate,
    endDate: $endDate,
    documentType: $documentType,
    documentSubType: $documentSubType
  ) {
    inWarehouse
    gasStation
    carWash
    gasAndCarWash
    receipts {
      warehouseName
      receiptType
      documentType
      transactionDateTime
      transactionDate
      companyNumber
      warehouseNumber
      operatorNumber
      warehouseShortName
      registerNumber
      transactionNumber
      transactionType
      transactionBarcode
      total
      warehouseAddress1
      warehouseAddress2
      warehouseCity
      warehouseState
      warehouseCountry
      warehousePostalCode
      totalItemCount
      subTotal
      taxes
      invoiceNumber
      sequenceNumber
      itemArray {
        itemNumber
        itemDescription01
        frenchItemDescription1
        itemDescription02
        frenchItemDescription2
        itemIdentifier
        itemDepartmentNumber
        unit
        amount
        taxFlag
        merchantID
        entryMethod
        transDepartmentNumber
        fuelUnitQuantity
        fuelGradeCode
        itemUnitPriceAmount
        fuelUomCode
        fuelUomDescription
        fuelUomDescriptionFr
        fuelGradeDescription
        fuelGradeDescriptionFr
      }
      tenderArray {
        tenderTypeCode
        tenderSubTypeCode
        tenderDescription
        amountTender
        displayAccountNumber
        sequenceNumber
        approvalNumber
        responseCode
        tenderTypeName
        transactionID
        merchantID
        entryMethod
        tenderAcctTxnNumber
        tenderAuthorizationCode
        tenderTypeNameFr
        tenderEntryMethodDescription
        walletType
        walletId
        storedValueBucket
      }
      couponArray {
        upcnumberCoupon
        voidflagCoupon
        refundflagCoupon
        taxflagCoupon
        amountCoupon
      }
      subTaxes {
        tax1
        tax2
        tax3
        tax4
        aTaxPercent
        aTaxLegend
        aTaxAmount
        aTaxPrintCode
        aTaxPrintCodeFR
        aTaxIdentifierCode
        bTaxPercent
        bTaxLegend
        bTaxAmount
        bTaxPrintCode
        bTaxPrintCodeFR
        bTaxIdentifierCode
        cTaxPercent
        cTaxLegend
        cTaxAmount
        cTaxIdentifierCode
        dTaxPercent
        dTaxLegend
        dTaxAmount
        dTaxPrintCode
        dTaxPrintCodeFR
        dTaxIdentifierCode
        uTaxLegend
        uTaxAmount
        uTaxableAmount
      }
      instantSavings
      membershipNumber
    }
  }
}
""".replace("\n", " ").strip()

RECEIPTS_SUMMARY_QUERY = RECEIPTS_QUERY
RECEIPT_DETAIL_QUERY = RECEIPTS_QUERY


RECEIPTS_SCRIPT = """
            const payload = arguments[0];
            const headers = arguments[1];
            const callback = arguments[arguments.length - 1];

            const xhr = new XMLHttpRequest();
            xhr.open('POST', arguments[2], true);
            xhr.responseType = 'json';
            xhr.timeout = 120000;

            for (const [key, value] of Object.entries(headers)) {
                xhr.setRequestHeader(key, value);
            }

            xhr.onload = function() {
                callback({
                    ok: true,
                    status: xhr.status,
                    response: xhr.response,
                });
            };
            xhr.onerror = function() {
                callback({
                    ok: false,
                    kind: 'error',
                    status: xhr.status,
                    response: xhr.response,
                });
            };
            xhr.ontimeout = function() {
                callback({
                    ok: false,
                    kind: 'timeout',
                    status: xhr.status,
                });
            };

            xhr.send(JSON.stringify(payload));
            """

STORAGE_SCRIPT = """
            const dump = (storage) => Object.fromEntries(
                Object.keys(storage).map((key) => [key, storage.getItem(key)])
            );
            return {
                localStorage: dump(window.localStorage),
                sessionStorage: dump(window.sessionStorage),
            };
            """


def quarter_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """Split start..end (both inclusive) into chunks of three months, the last one capped at end."""
    ranges: list[tuple[date, date]] = []
    current = start
    while current <= end:
        chunk_end = min(shift_months(current, CHUNK_MONTHS) - timedelta(days=1), end)
        ranges.append((current, chunk_end))
        current = shift_months(current, CHUNK_MONTHS)
    return ranges


@register
class CostcoConnector(VendorConnector):
    """Costco warehouse receipt acquisition connector."""

    key = "costco"
    label = "Costco"
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
        """Sign in and download the provider's Costco receipts that it does not hold yet."""
        driver.get(ACCOUNT_HOME_URL)
        if not self.signed_in(driver):
            driver = self._sign_in_with_clean_chrome(prompt)
        self.ensure_account_home(driver)
        return self.download_new(driver, settings.receipts_dir, provider_key, held, start, end)

    def _sign_in_with_clean_chrome(self, prompt: Prompt) -> Any:
        """Sign in via an un-debugged Chrome so Akamai Bot Manager allows authentication.

        Closes the debugged Chrome to release the profile, opens a clean Chrome on the
        account home page for the user to complete sign-in, terminates the clean Chrome,
        and re-attaches with debugged Chrome.
        """
        self.close()
        proc = self.launch_clean_browser(ACCOUNT_HOME_URL)

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
        driver.get(ACCOUNT_HOME_URL)

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.signed_in(driver):
                break
            time.sleep(0.5)
        if not self.signed_in(driver):
            raise NeedsUserAction(SIGN_IN_NOT_DONE)

        return driver

    def signed_in(self, driver: Any) -> bool:
        """Whether the browser session has valid Costco authentication tokens."""
        try:
            url = driver.current_url or ""
            if "signin.costco.com" in url or "LogonForm" in url:
                return False
            return bool(self.get_id_token(driver) and self.get_client_id(driver))
        except (WebDriverException, JavascriptException):
            return False

    def ensure_account_home(self, driver: Any) -> None:
        """Navigate to the account home page if not already there."""
        url = driver.current_url or ""
        if "costco.com/myaccount" not in url:
            driver.get(ACCOUNT_HOME_URL)

    def get_id_token(self, driver: Any) -> str | None:
        """Retrieve the ID token from local storage or cached session storage."""
        try:
            token = driver.execute_script("return localStorage.getItem('idToken')")
            if token:
                return str(token)
            return _cached_id_token(driver)
        except (WebDriverException, JavascriptException):
            return None

    def get_client_id(self, driver: Any) -> str | None:
        """Retrieve the client ID from local storage."""
        try:
            client_id = driver.execute_script(
                "return localStorage.getItem('clientID') || localStorage.getItem('clientId')"
            )
            if client_id:
                return str(client_id)
            return _cached_client_id(driver)
        except (WebDriverException, JavascriptException):
            return None

    def get_client_identifier(self, driver: Any) -> str:
        """Resolve the client app identifier from the page runtime or fallback to constant."""
        try:
            val = driver.execute_script(
                """
                return window.costco?.clientIdentifier
                    || window.__INITIAL_STATE__?.clientIdentifier
                    || localStorage.getItem('clientIdentifier')
                    || localStorage.getItem('Client-Identifier')
                    || null;
                """
            )
            if val and isinstance(val, str) and val.strip():
                return val.strip()
        except (WebDriverException, JavascriptException):
            pass
        return CLIENT_IDENTIFIER

    def download_new(
        self,
        driver: Any,
        receipts_dir: Path,
        provider_key: str,
        held: Mapping[str, Path | None],
        start: date,
        end: date,
    ) -> list[Path]:
        """Download and write the receipts of the range that the provider does not hold."""
        id_token = self.get_id_token(driver)
        client_id = self.get_client_id(driver)
        if not id_token or not client_id:
            raise NeedsUserAction(SESSION_MISSING)
        client_identifier = self.get_client_identifier(driver)
        headers = _graphql_headers(client_id, id_token, client_identifier)

        vendor = get_vendor("costco")
        seen: set[str] = set()
        written: list[Path] = []
        for chunk_start, chunk_end in quarter_ranges(start, end):
            for receipt in self.fetch_chunk(driver, headers, chunk_start, chunk_end):
                barcode = _text(receipt.get("transactionBarcode"))
                if not barcode or barcode in seen:
                    continue
                seen.add(barcode)
                if barcode in held:
                    continue
                try:
                    target = vendor.receipt_file(receipts_dir, provider_key, _day(receipt), barcode)
                except ValueError:
                    raise ConnectorError(
                        "Costco returned a receipt whose barcode cannot be a file name"
                    ) from None
                _write_json(target, receipt)
                written.append(target)
        return written

    def fetch_chunk(
        self,
        driver: Any,
        headers: dict[str, str],
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """Fetch receipts for a chunk period."""
        payload = {
            "query": RECEIPTS_QUERY,
            "variables": {
                "startDate": f"{start.month}/{start.day:02d}/{start.year}",
                "endDate": f"{end.month}/{end.day:02d}/{end.year}",
                "documentType": "all",
                "documentSubType": "all",
            },
        }
        driver.set_script_timeout(GRAPHQL_TIMEOUT)
        response = driver.execute_async_script(RECEIPTS_SCRIPT, payload, headers, GRAPHQL_URL)
        return _parse_receipts_response(response)


def _parse_receipts_response(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        raise ConnectorError("Costco's receipts request returned no response")
    if not response.get("ok"):
        if response.get("kind") == "timeout":
            raise NeedsUserAction("Costco did not respond in time; sync again")
        raise _http_error(response.get("status"))
    if response.get("status") != 200:
        raise _http_error(response.get("status"))

    data = response.get("response")
    if not isinstance(data, dict):
        raise ConnectorError("Costco's receipts response was not JSON")
    if "errors" in data:
        raise ConnectorError("Costco's receipts query returned errors")
    inner = data.get("data") or {}
    counts = inner.get("receiptsWithCounts") if isinstance(inner, dict) else None
    receipts = counts.get("receipts") if isinstance(counts, dict) else None
    receipts = receipts or []
    if not all(isinstance(receipt, dict) for receipt in receipts):
        raise ConnectorError("Costco's receipts response had an unexpected shape")
    return receipts


def _http_error(status: Any) -> Exception:
    if status in (401, 403):
        return NeedsUserAction("Costco rejected the session; sign in again, then sync")
    return ConnectorError(f"Costco's receipts request failed (status {status})")


def _graphql_headers(
    client_id: str, id_token: str, client_identifier: str = CLIENT_IDENTIFIER
) -> dict[str, str]:
    return {
        "Content-Type": "application/json-patch+json",
        "Costco.Env": "ecom",
        "Costco.Service": "restOrders",
        "Costco-X-Wcs-Clientid": client_id,
        "Client-Identifier": client_identifier,
        "Costco-X-Authorization": f"Bearer {id_token}",
    }


def _cached_id_token(driver: Any) -> str | None:
    """The ID token from storage, when the short key is missing. Tokens stay in memory."""
    snapshot = driver.execute_script(STORAGE_SCRIPT) or {}
    for storage_name in ("localStorage", "sessionStorage"):
        storage = snapshot.get(storage_name) or {}
        if not isinstance(storage, dict):
            continue
        direct = storage.get("idToken")
        if direct:
            return str(direct)
        for key, raw in storage.items():
            if "idtoken" not in str(key).lower() or not raw:
                continue
            secret = _id_token_secret(raw)
            if secret:
                return secret
    return None


def _id_token_secret(raw: Any) -> str | None:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        isinstance(parsed, dict)
        and parsed.get("credentialType") == "IdToken"
        and parsed.get("secret")
    ):
        return str(parsed["secret"])
    return None


def _cached_client_id(driver: Any) -> str | None:
    snapshot = driver.execute_script(STORAGE_SCRIPT) or {}
    for storage_name in ("localStorage", "sessionStorage"):
        storage = snapshot.get(storage_name) or {}
        if not isinstance(storage, dict):
            continue
        for key, raw in storage.items():
            if str(key).lower() in ("clientid", "client_id", "wcs_clientid") and raw:
                return str(raw)
    return None


def _day(receipt: dict[str, Any]) -> date:
    try:
        return receipt_date(receipt)
    except ValueError:
        raise ConnectorError("Costco returned a receipt with no usable date") from None


def _write_json(target: Path, receipt: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    temporary.replace(target)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()
