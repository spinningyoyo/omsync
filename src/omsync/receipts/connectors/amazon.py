"""Amazon order and refund acquisition through an authenticated Chrome session."""

import contextlib
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from selenium.common.exceptions import JavascriptException, WebDriverException
from selenium.webdriver.common.by import By

from omsync.core import money
from omsync.core.settings import Settings
from omsync.core.vault import LoginCredentials, Vault
from omsync.ingest.connectors.base import ConnectorError, NeedsUserAction, Prompt, SyncCancelled
from omsync.receipts.connectors.base import VendorConnector
from omsync.receipts.connectors.registry import register

logger = logging.getLogger(__name__)

ACCOUNT_TRANSACTIONS_URL = "https://www.amazon.com/cpe/yourpayments/transactions"
INVOICE_URL = "https://www.amazon.com/gp/css/summary/print.html?orderID={order_id}"
RECEIPTS_FOLDER = "AMAZON"
BROWSER_MODE = "chrome-attach"

SIGN_IN_QUESTION = (
    "Sign in to Amazon in the browser window (including any verification code), then press Enter"
)
SIGN_IN_TIMED_OUT = "no Enter came in time for the Amazon sign-in; sync again"
SIGN_IN_NOT_DONE = "Amazon sign-in was not completed in the browser window; sign in and sync again"
SESSION_MISSING = "Amazon did not return a session or UPX token; sign in again, then sync"

INITIAL_DATA_SCRIPT = """
const props = window.__NEXT_DATA__?.props?.pageProps;
return {
    token: props?.token || null,
    transactionList: props?.transactionList || null,
    lastEvaluatedKey: props?.lastEvaluatedPageKey || null,
};
"""

GET_TRANSACTIONS_SCRIPT = """
const payload = arguments[0];
const token = arguments[1];
const callback = arguments[arguments.length - 1];

fetch("https://www.amazon.com/payments-portal/data/iris/live/v1/data/manage/get-transactions", {
    method: "POST",
    headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-amzn-upx-token": token,
    },
    body: JSON.stringify(payload),
})
.then(r => r.json())
.then(data => callback({ ok: true, data }))
.catch(err => callback({ ok: false, error: String(err) }));
"""

GET_INVOICE_SCRIPT = """
const url = arguments[0];
const callback = arguments[arguments.length - 1];

fetch(url)
.then(r => {
    if (!r.ok) return callback({ ok: false, status: r.status });
    return r.text().then(html => callback({ ok: true, html }));
})
.catch(err => callback({ ok: false, error: String(err) }));
"""


@register
class AmazonConnector(VendorConnector):
    """Amazon order and refund acquisition connector."""

    key = "amazon"
    label = "Amazon"
    browser_mode = "chrome-attach"

    def download_receipts(
        self,
        driver: Any,
        settings: Settings,
        prompt: Prompt,
        *,
        start: date | None = None,
        end: date | None = None,
        account: str = "default",
        vault: Vault | None = None,
        vault_item_id: str | None = None,
    ) -> list[Path]:
        """Sign in and download Amazon order and refund JSON receipt files."""
        credentials = None
        if vault is not None and vault_item_id is not None:
            credentials = vault.get_login(vault_item_id)
        self.sign_in(driver, prompt, credentials)
        root = settings.receipts_dir / RECEIPTS_FOLDER / account
        return self.download_new(driver, root, start=start, end=end)

    def sign_in(
        self,
        driver: Any,
        prompt: Prompt,
        credentials: LoginCredentials | None = None,
    ) -> None:
        """Sign in automatically with vault credentials, or wait for manual sign-in."""
        driver.get(ACCOUNT_TRANSACTIONS_URL)
        if self.signed_in(driver):
            return

        if credentials is not None and credentials.username and credentials.password:
            self._attempt_auto_login(driver, prompt, credentials)
            if self.signed_in(driver):
                return

        try:
            confirmed = prompt.confirm(SIGN_IN_QUESTION, button="I have signed in")
        except (TimeoutError, SyncCancelled):
            raise NeedsUserAction(SIGN_IN_TIMED_OUT) from None
        if not confirmed:
            raise NeedsUserAction(SIGN_IN_TIMED_OUT)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.signed_in(driver):
                return
            time.sleep(0.5)
        raise NeedsUserAction(SIGN_IN_NOT_DONE)

    def _attempt_auto_login(
        self, driver: Any, prompt: Prompt, credentials: LoginCredentials
    ) -> None:
        try:
            email_field = self._find_first(
                driver, ["#ap_email", "input[name='email']", "input[type='email']"]
            )
            if email_field and credentials.username:
                email_field.clear()
                for ch in credentials.username:
                    email_field.send_keys(ch)
                    time.sleep(0.02)
                continue_btn = self._find_first(driver, ["#continue", "input#continue"])
                if continue_btn:
                    continue_btn.click()
                time.sleep(1.5)

            pass_field = self._find_first(
                driver, ["#ap_password", "input[name='password']", "input[type='password']"]
            )
            if pass_field and credentials.password:
                pass_field.clear()
                for ch in credentials.password:
                    pass_field.send_keys(ch)
                    time.sleep(0.02)
                time.sleep(0.5)
                remember_me = self._find_first(driver, ["input[name='rememberMe']"])
                if remember_me and not remember_me.is_selected():
                    with contextlib.suppress(WebDriverException):
                        remember_me.click()
                signin_btn = self._find_first(driver, ["#signInSubmit", "input#signInSubmit"])
                if signin_btn:
                    signin_btn.click()
                time.sleep(2.0)

            self._handle_mfa(driver, prompt)
        except (WebDriverException, JavascriptException):
            pass

    def _handle_mfa(self, driver: Any, prompt: Prompt) -> None:
        otp_field = self._find_first(driver, ["#auth-mfa-otpcode", "input[name='otpCode']"])
        if otp_field:
            code = prompt.ask("Enter Amazon OTP code", digits=6)
            otp_field.clear()
            otp_field.send_keys(code)
            submit_otp = self._find_first(driver, ["#auth-signin-button", "input[type='submit']"])
            if submit_otp:
                submit_otp.click()
            time.sleep(2.0)

        body_text = (driver.page_source or "").lower()
        if "notification" in body_text or "approve" in body_text:
            approval_msg = (
                "Approve the notification in your Amazon mobile app, then select I have approved"
            )
            prompt.confirm(approval_msg, button="I have approved")

    def _find_first(self, driver: Any, selectors: list[str]) -> Any | None:
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements and elements[0].is_displayed():
                    return elements[0]
            except WebDriverException:
                continue
        return None

    def signed_in(self, driver: Any) -> bool:
        """Whether the browser is currently at the authenticated transactions page."""
        url = driver.current_url or ""
        return "cpe/yourpayments/transactions" in url and "signin" not in url.lower()

    def download_new(
        self,
        driver: Any,
        root: Path,
        *,
        start: date | None,
        end: date | None,
    ) -> list[Path]:
        """Fetch transactions and invoices, then write individual order JSON receipt files."""
        initial = driver.execute_script(INITIAL_DATA_SCRIPT) or {}
        token = initial.get("token")
        if not token:
            raise NeedsUserAction(SESSION_MISSING)

        transactions = self.fetch_transactions(
            driver,
            token,
            initial_list=initial.get("transactionList"),
            next_key=initial.get("lastEvaluatedKey"),
            start=start,
        )

        scoped = _filter_by_date(transactions, start, end)
        if not scoped:
            return []

        orders_map = _group_by_order(scoped)
        existing_files = _existing_order_files(root)

        written: list[Path] = []
        for oid, order_info in sorted(orders_map.items()):
            existing_path = existing_files.get(oid)
            existing_data = _read_json(existing_path) if existing_path else None

            if existing_data and not _has_new_activity(existing_data, order_info):
                continue

            inv = (
                existing_data
                if existing_data and existing_data.get("items")
                else self.fetch_invoice(driver, oid)
            )

            charges = _merge_charges(existing_data, order_info.get("charges", []))
            refunds = _merge_refunds(existing_data, order_info.get("refunds", []))

            order_date_str = (
                inv.get("order_date")
                or (charges[0]["date"] if charges else None)
                or (refunds[0]["date"] if refunds else None)
                or date.today().isoformat()
            )
            order_day = _parse_date(order_date_str) or date.today()

            total_cents = sum(c["amount_cents"] for c in charges)
            inv_items = inv.get("items", [])
            items = _build_json_items(inv_items, total_cents)

            order_json = {
                "order_id": oid,
                "order_date": order_date_str,
                "total_cents": total_cents,
                "tax_cents": inv.get("tax_cents", 0),
                "items": items,
                "charges": charges,
                "refunds": refunds,
            }

            stem = _file_stem(oid)
            target = root / str(order_day.year) / f"{stem}.json"
            _write_json(target, order_json)
            written.append(target)

        return written

    def fetch_transactions(
        self,
        driver: Any,
        token: str,
        *,
        initial_list: list[dict[str, Any]] | None,
        next_key: str | None,
        start: date | None,
    ) -> list[dict[str, Any]]:
        """Paginate transactions from Amazon Payments Iris API."""
        transactions: list[dict[str, Any]] = []
        if initial_list:
            transactions.extend(initial_list)

        cursor = next_key
        while cursor:
            if start is not None and transactions:
                oldest = _parse_date(transactions[-1].get("formattedDate", ""))
                if oldest is not None and oldest < start:
                    break

            payload: dict[str, Any] = {
                "type": "GetTransactions",
                "locale": "en_US",
                "surfaceInfo": {
                    "surfaceType": "desktop",
                    "clientApplicationType": "browser",
                    "surfaceFeatures": [],
                    "userAgent": driver.execute_script("return navigator.userAgent;"),
                },
                "requestTraceId": f"OMS{int(time.time())}",
                "applicationInstanceTraceId": f"OMSINST{int(time.time())}",
                "transactionsViewRequest": {"filtersControls": {"includeFilters": True}},
                "widgetName": "ViewTransactions",
                "exclusiveStartKey": cursor,
            }

            reply = driver.execute_async_script(GET_TRANSACTIONS_SCRIPT, payload, token)
            if not reply or not reply.get("ok"):
                break

            data = reply.get("data", {})
            display = data.get("displayResponse", {})
            batch = display.get("transactionsList", [])
            if not batch:
                break

            transactions.extend(batch)
            cursor = display.get("lastEvaluatedKey")
            time.sleep(0.15)

        return transactions

    def fetch_invoice(self, driver: Any, order_id: str) -> dict[str, Any]:
        """Fetch and parse printable invoice HTML for an order."""
        url = INVOICE_URL.format(order_id=order_id)
        reply = driver.execute_async_script(GET_INVOICE_SCRIPT, url)
        if reply and reply.get("ok") and reply.get("html"):
            return _parse_invoice_html(reply["html"], order_id)
        return {"order_id": order_id, "items": [], "tax_cents": 0, "order_date": None}


def _group_by_order(transactions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    orders: dict[str, dict[str, Any]] = {}
    for tx in transactions:
        amount_raw = tx.get("formattedAmount", "")
        cents, is_refund = _parse_cents(amount_raw)
        dt = _parse_date(tx.get("formattedDate", ""))
        iso_date = dt.isoformat() if dt else ""
        card_last4 = _card_last4(tx.get("paymentMethodDisplayStringData"))

        for order in tx.get("orderData", []):
            oid = _extract_order_id(order.get("orderDisplayString", ""))
            if not oid:
                continue
            if oid not in orders:
                orders[oid] = {"charges": [], "refunds": []}

            if is_refund:
                orders[oid]["refunds"].append(
                    {
                        "amount_cents": cents,
                        "card_last4": card_last4,
                        "date": iso_date,
                        "reason": "Customer Return",
                    }
                )
            else:
                orders[oid]["charges"].append(
                    {
                        "amount_cents": cents,
                        "card_last4": card_last4,
                        "date": iso_date,
                    }
                )
    return orders


def _filter_by_date(
    transactions: list[dict[str, Any]], start: date | None, end: date | None
) -> list[dict[str, Any]]:
    scoped: list[dict[str, Any]] = []
    for tx in transactions:
        tx_date = _parse_date(tx.get("formattedDate", ""))
        if tx_date is None:
            continue
        if start is not None and tx_date < start:
            continue
        if end is not None and tx_date > end:
            continue
        scoped.append(tx)
    return scoped


def _has_new_activity(existing_data: dict[str, Any], current_info: dict[str, Any]) -> bool:
    existing_charges = {
        (c.get("amount_cents"), c.get("date"), c.get("card_last4"))
        for c in existing_data.get("charges", [])
    }
    for c in current_info.get("charges", []):
        if (c.get("amount_cents"), c.get("date"), c.get("card_last4")) not in existing_charges:
            return True

    existing_refunds = {
        (r.get("amount_cents"), r.get("date"), r.get("card_last4"))
        for r in existing_data.get("refunds", [])
    }
    for r in current_info.get("refunds", []):
        if (r.get("amount_cents"), r.get("date"), r.get("card_last4")) not in existing_refunds:
            return True
    return False


def _merge_charges(
    existing_data: dict[str, Any] | None, new_charges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str | None]] = set()
    source = (existing_data.get("charges", []) if existing_data else []) + new_charges
    for c in source:
        key = (int(c.get("amount_cents", 0)), str(c.get("date", "")), c.get("card_last4"))
        if key not in seen:
            seen.add(key)
            merged.append(
                {
                    "amount_cents": key[0],
                    "card_last4": key[2],
                    "date": key[1],
                }
            )
    return merged


def _merge_refunds(
    existing_data: dict[str, Any] | None, new_refunds: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str | None]] = set()
    source = (existing_data.get("refunds", []) if existing_data else []) + new_refunds
    for r in source:
        key = (int(r.get("amount_cents", 0)), str(r.get("date", "")), r.get("card_last4"))
        if key not in seen:
            seen.add(key)
            merged.append(
                {
                    "amount_cents": key[0],
                    "card_last4": key[2],
                    "date": key[1],
                    "reason": str(r.get("reason", "Customer Return")),
                }
            )
    return merged


def _build_json_items(
    invoice_items: list[dict[str, Any]], total_cents: int
) -> list[dict[str, Any]]:
    if not invoice_items:
        return [{"title": "Amazon Order", "amount_cents": total_cents, "quantity": 1}]

    item_prices = [it.get("price_cents", 0) for it in invoice_items]
    subtotal = sum(item_prices)
    allocated = (
        money.allocate(total_cents, item_prices)
        if subtotal > 0
        else [total_cents // len(invoice_items)] * len(invoice_items)
    )
    items: list[dict[str, Any]] = []
    for it, item_total in zip(invoice_items, allocated, strict=True):
        items.append(
            {
                "title": it.get("title", "Amazon Item"),
                "amount_cents": item_total if item_total > 0 else total_cents,
                "quantity": it.get("quantity", 1),
            }
        )
    return items


def _comp(html: str, name: str) -> str:
    m = re.search(rf"data-component=[\"\']{name}[\"\'][^>]*>(.*?)</div>", html, re.DOTALL)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() if m else ""


def _all_comps(html: str, name: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
        for m in re.finditer(
            rf"data-component=[\"\']{name}[\"\'][^>]*>(.*?)</div>", html, re.DOTALL
        )
    ]


def _parse_tax_cents(html: str) -> int:
    tax_match = re.search(r"tax to be collected:.*?\$([\d,.]+)", html, re.IGNORECASE)
    if tax_match:
        try:
            return money.to_cents(tax_match.group(1))
        except ValueError:
            return 0
    return 0


def _parse_items(html: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    titles = _all_comps(html, "itemTitle")
    prices = _all_comps(html, "unitPrice")
    quantities = _all_comps(html, "quantity")

    for i, title in enumerate(titles):
        price_cents = 0
        if i < len(prices):
            m_price = re.search(r"\$([\d,.]+)", prices[i])
            if m_price:
                try:
                    price_cents = money.to_cents(m_price.group(1))
                except ValueError:
                    price_cents = 0
        qty = 1
        if i < len(quantities):
            m_qty = re.search(r"\d+", quantities[i])
            if m_qty:
                qty = int(m_qty.group(0))
        items.append({"title": title, "price_cents": price_cents, "quantity": qty})
    return items


def _parse_invoice_html(html: str, fallback_order_id: str) -> dict[str, Any]:
    oid = _comp(html, "orderId") or fallback_order_id
    raw_date = _comp(html, "orderDate")
    parsed_date = _parse_date(raw_date)

    return {
        "order_id": oid,
        "order_date": parsed_date.isoformat() if parsed_date else None,
        "tax_cents": _parse_tax_cents(html),
        "items": _parse_items(html),
    }


def _parse_date(value: str) -> date | None:
    cleaned = value.strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _card_last4(pm_data: dict[str, Any] | None) -> str | None:
    if not pm_data:
        return None
    digits = (pm_data.get("paymentMethodNumber") or {}).get("lastDigits", "").strip()
    return digits or None


def _extract_order_id(display: str) -> str:
    cleaned = display.strip()
    if "Order #" in cleaned:
        cleaned = cleaned.replace("Order #", "").strip()
    return cleaned


def _existing_order_files(root: Path) -> dict[str, Path]:
    if not root.exists():
        return {}
    return {
        path.stem: path
        for path in root.rglob("*.json")
        if path.is_file() and not path.name.startswith(".") and not path.parent.name.startswith(".")
    }


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _file_stem(order_id: str) -> str:
    """The order ID as a file name. Anything that could name another folder is refused."""
    if (
        order_id != Path(order_id).name
        or order_id in (".", "..")
        or "\\" in order_id
        or "\0" in order_id
    ):
        raise ConnectorError("Amazon returned an order whose ID cannot be a file name")
    return order_id


def _write_json(target: Path, receipt: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    temporary.replace(target)


def _parse_cents(amount_str: str) -> tuple[int, bool]:
    cleaned = amount_str.strip()
    is_refund = cleaned.startswith("+")
    num_str = cleaned.lstrip("+-").replace("$", "").replace(",", "").strip()
    try:
        return money.to_cents(num_str), is_refund
    except ValueError:
        return 0, is_refund
