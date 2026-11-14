"""Costco warehouse receipt acquisition through an authenticated Chrome session."""

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from selenium.common.exceptions import TimeoutException, WebDriverException

from omsync.core.months import shift_months
from omsync.core.settings import Settings
from omsync.ingest.connectors.banks.browser import BrowserSession
from omsync.ingest.connectors.base import ConnectorError, NeedsUserAction, Prompt
from omsync.receipts.vendors.costco import RECEIPTS_FOLDER, quarter_folder, receipt_date

ACCOUNT_HOME_URL = "https://www.costco.com/my/AccountHomeView"
GRAPHQL_URL = "https://ecom-api.costco.com/ebusiness/order/v1/orders/graphql"
CLIENT_IDENTIFIER = "481b1aec-aa3b-454b-b81b-48187e28f205"  # hardcoded in the site's web app
SIGNED_IN_MARKER = "costco.com/my/"  # the page is signed in once its URL contains this
GRAPHQL_TIMEOUT = 120  # seconds, for the browser request and the script timeout
CHUNK_MONTHS = 3
BROWSER_MODE = "chrome-manual"

SIGN_IN_QUESTION = (
    "Sign in to Costco in the browser window (including any verification code), then press Enter"
)
SIGN_IN_NOT_DONE = "Costco sign-in was not completed in the browser window; sign in and sync again"
SIGN_IN_TIMED_OUT = "no Enter came in time for the Costco sign-in; sync again"
SESSION_MISSING = "Costco did not return a session; sign in again, then sync"

RECEIPTS_QUERY = """
query receipts($startDate: String!, $endDate: String!) {
  receipts(startDate: $startDate, endDate: $endDate) {
    warehouseName
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
    itemArray {
      itemNumber
      itemDescription01
      frenchItemDescription1
      itemDescription02
      frenchItemDescription2
      itemIdentifier
      unit
      amount
      taxFlag
      merchantID
      entryMethod
    }
    tenderArray {
      tenderTypeCode
      tenderDescription
      amountTender
      displayAccountNumber
      sequenceNumber
      approvalNumber
      responseCode
      transactionID
      merchantID
      entryMethod
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
      bTaxPercent
      bTaxLegend
      bTaxAmount
      cTaxPercent
      cTaxLegend
      cTaxAmount
      dTaxAmount
    }
    instantSavings
    membershipNumber
  }
}
""".replace("\n", " ").strip()

# Run inside the page so its authenticated cookies and session apply to the request.
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


def fetch_receipts(settings: Settings, prompt: Prompt, *, start: date, end: date) -> list[Path]:
    """Save each Costco receipt from start to end (inclusive) that is not on disk yet.

    The browser opens the account page. You sign in there and press Enter at the prompt. Returns
    the files this call wrote. Raises NeedsUserAction when the sign-in or the session needs you, and
    ConnectorError for anything else, such as a Chrome that is not listening for debugging. No
    message holds a token or the membership number.
    """
    if start > end:
        raise ValueError("start must not be after end")
    root = settings.receipts_dir / RECEIPTS_FOLDER
    with tempfile.TemporaryDirectory(prefix="omsync-costco-") as scratch:
        session = BrowserSession()
        session.prepare(download_dir=Path(scratch), profile_dir=settings.browser_profile_dir)
        try:
            driver = session.get_driver(BROWSER_MODE)
            _sign_in(driver, prompt)
            return _download_new(driver, root, start, end)
        except TimeoutException:
            raise NeedsUserAction("Costco did not respond in time; sync again") from None
        except WebDriverException as exc:
            raise ConnectorError(
                f"the browser stopped while fetching Costco receipts ({type(exc).__name__})"
            ) from None
        finally:
            session.close()


def quarter_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """Split start..end (both inclusive) into chunks of three months, the last one capped at end."""
    ranges: list[tuple[date, date]] = []
    current = start
    while current <= end:
        chunk_end = min(shift_months(current, CHUNK_MONTHS) - timedelta(days=1), end)
        ranges.append((current, chunk_end))
        current = shift_months(current, CHUNK_MONTHS)
    return ranges


def _sign_in(driver: Any, prompt: Prompt) -> None:
    """Wait for manual sign-in unless the attached browser already has an account session."""
    if _on_account_home(driver):
        return
    driver.get(ACCOUNT_HOME_URL)
    if _on_account_home(driver):
        return
    try:
        prompt.ask(SIGN_IN_QUESTION)
    except TimeoutError:
        raise NeedsUserAction(SIGN_IN_TIMED_OUT) from None
    if not _on_account_home(driver):
        raise NeedsUserAction(SIGN_IN_NOT_DONE)


def _on_account_home(driver: Any) -> bool:
    return SIGNED_IN_MARKER in (driver.current_url or "")


def _download_new(driver: Any, root: Path, start: date, end: date) -> list[Path]:
    id_token = driver.execute_script("return localStorage.getItem('idToken')")
    if not id_token:
        id_token = _cached_id_token(driver)
    client_id = driver.execute_script("return localStorage.getItem('clientID')")
    if not id_token or not client_id:
        raise NeedsUserAction(SESSION_MISSING)
    headers = _graphql_headers(client_id, id_token)

    existing = _existing_barcodes(root)
    seen: set[str] = set()
    written: list[Path] = []
    for chunk_start, chunk_end in quarter_ranges(start, end):
        for receipt in _fetch_chunk(driver, headers, chunk_start, chunk_end):
            barcode = _text(receipt.get("transactionBarcode"))
            if not barcode or barcode in seen:
                continue
            seen.add(barcode)
            stem = _file_stem(barcode)
            if stem in existing:
                continue
            day = _day(receipt)
            target = root / str(day.year) / quarter_folder(day) / f"{stem}.json"
            _write_json(target, receipt)
            written.append(target)
    return written


def _fetch_chunk(
    driver: Any, headers: dict[str, str], start: date, end: date
) -> list[dict[str, Any]]:
    payload = {
        "query": RECEIPTS_QUERY,
        "variables": {
            "startDate": start.strftime("%m/%d/%Y"),
            "endDate": end.strftime("%m/%d/%Y"),
        },
    }
    driver.set_script_timeout(GRAPHQL_TIMEOUT)
    response = driver.execute_async_script(RECEIPTS_SCRIPT, payload, headers, GRAPHQL_URL)
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
    receipts = inner.get("receipts") if isinstance(inner, dict) else None
    receipts = receipts or []
    if not all(isinstance(receipt, dict) for receipt in receipts):
        raise ConnectorError("Costco's receipts response had an unexpected shape")
    return receipts


def _http_error(status: Any) -> Exception:
    if status in (401, 403):
        return NeedsUserAction("Costco rejected the session; sign in again, then sync")
    return ConnectorError(f"Costco's receipts request failed (status {status})")


def _graphql_headers(client_id: str, id_token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json-patch+json",
        "Costco.Env": "ecom",
        "Costco.Service": "restOrders",
        "Costco-X-Wcs-Clientid": client_id,
        "Client-Identifier": CLIENT_IDENTIFIER,
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


def _existing_barcodes(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.stem for path in root.rglob("*.json") if path.is_file()}


def _day(receipt: dict[str, Any]) -> date:
    try:
        return receipt_date(receipt)
    except ValueError:
        raise ConnectorError("Costco returned a receipt with no usable date") from None


def _file_stem(barcode: str) -> str:
    """The barcode as a file name. Anything that could name another folder is refused."""
    if (
        barcode != Path(barcode).name
        or barcode in (".", "..")
        or "\\" in barcode
        or "\0" in barcode
    ):
        raise ConnectorError("Costco returned a receipt whose barcode cannot be a file name")
    return barcode


def _write_json(target: Path, receipt: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    temporary.replace(target)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()
