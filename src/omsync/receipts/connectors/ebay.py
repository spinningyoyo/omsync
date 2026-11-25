"""eBay order and receipt acquisition through an authenticated Chrome session."""

import contextlib
import json
import logging
import re
import time
from collections.abc import Mapping
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
from omsync.receipts.vendors import get_vendor

logger = logging.getLogger(__name__)

PURCHASE_HISTORY_URL = "https://www.ebay.com/mye/myebay/purchase"
PURCHASE_API_URL = "https://www.ebay.com/mye/myebay/ajax/v2/purchase/mp/get"
MODULE_ID = "122164"

SIGN_IN_QUESTION = (
    "Sign in to eBay in the browser window (including any verification code), then press Enter"
)
SIGN_IN_TIMED_OUT = "no Enter came in time for the eBay sign-in; sync again"
SIGN_IN_NOT_DONE = "eBay sign-in was not completed in the browser window; sign in and sync again"

GET_PURCHASES_SCRIPT = """
const url = arguments[0];
const callback = arguments[arguments.length - 1];

fetch(url, {
    headers: {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*"
    }
})
.then(r => r.json())
.then(data => callback({ ok: true, data }))
.catch(err => callback({ ok: false, error: String(err) }));
"""


@register
class EbayConnector(VendorConnector):
    """eBay order acquisition connector."""

    key = "ebay"
    label = "eBay"
    uses_vault = True

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
        """Sign in and download the provider's eBay order receipt JSON files."""
        credentials = None
        if vault is not None and vault_item_id is not None:
            credentials = vault.get_login(vault_item_id)
        self.sign_in(driver, prompt, credentials)
        return self.download_new(
            driver, settings.receipts_dir, provider_key, held, start=start, end=end
        )

    def sign_in(
        self,
        driver: Any,
        prompt: Prompt,
        credentials: LoginCredentials | None = None,
    ) -> None:
        """Sign in automatically with vault credentials, or wait for manual sign-in."""
        driver.get(PURCHASE_HISTORY_URL)
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
            user_field = self._find_first(
                driver, ["#userid", "input[name='userid']", "input[type='email']"]
            )
            if user_field and credentials.username:
                user_field.clear()
                for ch in credentials.username:
                    user_field.send_keys(ch)
                    time.sleep(0.02)
                continue_selectors = ["#signin-continue-btn", "button#signin-continue-btn"]
                continue_btn = self._find_first(driver, continue_selectors)
                if continue_btn:
                    continue_btn.click()
                time.sleep(1.5)

            pass_field = self._find_first(
                driver, ["#pass", "input[name='pass']", "input[type='password']"]
            )
            if pass_field and credentials.password:
                pass_field.clear()
                for ch in credentials.password:
                    pass_field.send_keys(ch)
                    time.sleep(0.02)
                time.sleep(0.5)
                signin_btn = self._find_first(driver, ["#sgnBt", "button#sgnBt", "input#sgnBt"])
                if signin_btn:
                    signin_btn.click()
                time.sleep(2.0)
        except (WebDriverException, JavascriptException):
            pass

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
        """Whether the browser is currently at the authenticated purchase history page."""
        url = driver.current_url or ""
        return "mye/myebay/purchase" in url and "signin" not in url.lower()

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
        """Fetch purchase history and order details, writing JSON receipt files."""
        vendor = get_vendor(self.key)
        year_filters = _year_filters_for_range(start, end)
        written: list[Path] = []
        seen_order_ids: set[str] = set()

        for filter_name in year_filters:
            orders_to_fetch = self._fetch_filter_orders(
                driver, filter_name, start=start, end=end, held=held, seen=seen_order_ids
            )
            for summary in orders_to_fetch:
                oid = summary["order_id"]
                seen_order_ids.add(oid)

                order_data = self._fetch_order_details(
                    driver, summary["url"], fallback_id=oid, fallback_date=summary.get("date")
                )
                if not order_data:
                    continue

                order_day = _parse_order_day(order_data.get("order_date"))
                try:
                    target = vendor.receipt_file(receipts_dir, provider_key, order_day, oid)
                except ValueError:
                    err_msg = "eBay returned an order whose ID cannot be a file name"
                    raise ConnectorError(err_msg) from None

                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(order_data, indent=2), encoding="utf-8")
                written.append(target)

        logger.info("eBay wrote %d orders", len(written))
        return written

    def _fetch_filter_orders(
        self,
        driver: Any,
        filter_name: str,
        *,
        start: date,
        end: date,
        held: Mapping[str, Path | None],
        seen: set[str],
    ) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        page = 1
        has_more = True

        while has_more:
            url = (
                f"{PURCHASE_API_URL}?filter=year_filter:{filter_name}&page={page}"
                f"&modules=ALL_TRANSACTIONS&moduleId={MODULE_ID}&pg=purchase&mp=purchase-module-v2"
                f"&type=v2&_={int(time.time() * 1000)}"
            )
            reply = driver.execute_async_script(GET_PURCHASES_SCRIPT, url)
            if not reply or not reply.get("ok"):
                break

            river = reply.get("data", {}).get("modules", {}).get("RIVER", [{}])[0].get("data", {})
            items = river.get("items", [])
            if not items:
                break

            for it in items:
                if it.get("_type") != "PurchaseActivityCard":
                    continue
                oid = _extract_card_order_id(it)
                if not oid or oid in seen or oid in held:
                    continue
                order_date = _extract_card_date(it)
                if order_date is not None and (order_date < start or order_date > end):
                    continue
                order_url = _extract_card_details_url(it)
                if not order_url:
                    continue
                orders.append({"order_id": oid, "date": order_date, "url": order_url})

            next_link = river.get("pagination", {}).get("next", {})
            has_more = bool(next_link.get("action"))
            page += 1
            time.sleep(0.2)

        return orders

    def _fetch_order_details(
        self, driver: Any, url: str, fallback_id: str, fallback_date: date | None
    ) -> dict[str, Any] | None:
        try:
            driver.get(url)
            time.sleep(0.5)
            state = driver.execute_script("return window.__APP_INITIAL_STATE__ || null;")
            if state and isinstance(state, dict):
                return _extract_order_data(state, fallback_id, fallback_date)

            html = driver.page_source or ""
            marker = "window.__APP_INITIAL_STATE__ = "
            idx = html.find(marker)
            if idx != -1:
                sub = html[idx + len(marker) :]
                parsed, _ = json.JSONDecoder().raw_decode(sub)
                if isinstance(parsed, dict):
                    return _extract_order_data(parsed, fallback_id, fallback_date)
        except (WebDriverException, json.JSONDecodeError):
            logger.warning("Failed to fetch eBay order details for %s", fallback_id)
        return None


def _parse_order_day(date_str: str | None) -> date:
    if date_str:
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(date_str).date()
    return date.today()


def _year_filters_for_range(start: date, end: date) -> list[str]:
    today_year = date.today().year
    years_back = {
        0: "CURRENT_YEAR",
        1: "LAST_YEAR",
        2: "TWO_YEARS_AGO",
        3: "THREE_YEARS_AGO",
        4: "FOUR_YEARS_AGO",
        5: "FIVE_YEARS_AGO",
        6: "SIX_YEARS_AGO",
        7: "SEVEN_YEARS_AGO",
        8: "EIGHT_YEARS_AGO",
        9: "NINE_YEARS_AGO",
    }
    filters: list[str] = []
    for yr in range(end.year, start.year - 1, -1):
        diff = today_year - yr
        if 0 <= diff <= 9:
            filters.append(years_back[diff])
        elif diff > 9 and "ALL_YEARS" not in filters:
            filters.append("ALL_YEARS")
    return filters or ["CURRENT_YEAR"]


def _extract_card_order_id(card: dict[str, Any]) -> str | None:
    for ic in card.get("itemCards", []):
        oid = ic.get("__myb", {}).get("orderId")
        if oid:
            return str(oid).strip()
    sec = card.get("secondaryMessage", [])
    for idx, ts in enumerate(sec):
        text = _join_text_spans(ts)
        if "order number" in text.lower() and idx + 1 < len(sec):
            return _join_text_spans(sec[idx + 1]).strip()
    return None


def _extract_card_date(card: dict[str, Any]) -> date | None:
    sec = card.get("secondaryMessage", [])
    for idx, ts in enumerate(sec):
        text = _join_text_spans(ts)
        if "order date" in text.lower() and idx + 1 < len(sec):
            date_text = _join_text_spans(sec[idx + 1]).strip()
            return _parse_ebay_date(date_text)
    return None


def _extract_card_details_url(card: dict[str, Any]) -> str | None:
    for a in card.get("actions", []):
        act = a.get("action", {})
        if "VIEW_ORDER_DETAILS" in act.get("name", ""):
            url = act.get("URL")
            if url:
                return str(url)
    return None


def _parse_ebay_date(val: str) -> date | None:
    cleaned = re.sub(r"\s+at\s+.*$", "", val.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"^[A-Za-z]+,\s*", "", cleaned)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%Y-%m-%d"):
        with contextlib.suppress(ValueError):
            return datetime.strptime(cleaned, fmt).date()
    return None


def _join_text_spans(node: dict[str, Any]) -> str:
    spans = node.get("textSpans", [])
    return "".join(ts.get("text", "") for ts in spans if isinstance(ts, dict))


def _extract_order_info(
    order_info: dict[str, Any], fallback_id: str, fallback_date: date | None
) -> tuple[str, date | None, str]:
    order_id = fallback_id
    order_date = fallback_date
    seller = ""
    for sec in order_info.get("sections", []):
        for di in sec.get("dataItems", []):
            labels = [_join_text_spans(lb).lower() for lb in di.get("labels", [])]
            vals = [_join_text_spans(vl) for vl in di.get("values", [])]
            lbl = labels[0] if labels else ""
            val = vals[0] if vals else ""
            if "order number" in lbl and val:
                order_id = val.strip()
            elif ("time placed" in lbl or "placed on" in lbl) and val:
                parsed_d = _parse_ebay_date(val)
                if parsed_d:
                    order_date = parsed_d
            elif ("sold by" in lbl or "seller" in lbl) and val:
                seller = val.strip()
    return order_id, order_date, seller


def _extract_payment_totals(payment_info: dict[str, Any]) -> tuple[int, int, int, int]:
    subtotal_cents = 0
    shipping_cents = 0
    tax_cents = 0
    total_cents = 0

    for pli in payment_info.get("paymentLineItems", []):
        for di in pli.get("dataItems", []):
            labels = [_join_text_spans(lb).lower() for lb in di.get("labels", [])]
            vals = [_join_text_spans(vl) for vl in di.get("values", [])]
            lbl = labels[0] if labels else ""
            val = vals[0] if vals else ""
            if "item" in lbl:
                subtotal_cents = _parse_cents(val)
            elif "shipping" in lbl:
                shipping_cents = _parse_cents(val)
            elif "tax" in lbl:
                tax_cents = _parse_cents(val)

    tot_di = payment_info.get("orderTotal", {}).get("dataItems", [])
    for di in tot_di:
        vals = [_join_text_spans(vl) for vl in di.get("values", [])]
        if vals:
            total_cents = _parse_cents(vals[0])

    if total_cents == 0:
        total_cents = subtotal_cents + shipping_cents + tax_cents
    return subtotal_cents, shipping_cents, tax_cents, total_cents


def _extract_charges(
    payment_info: dict[str, Any], order_date: date | None, total_cents: int
) -> list[dict[str, Any]]:
    charges: list[dict[str, Any]] = []
    for inst in payment_info.get("paymentInstruments", []):
        desc = _join_text_spans(inst.get("description", {}))
        acc_text = inst.get("description", {}).get("accessibilityText") or ""
        amt_text = _join_text_spans(inst.get("chargedAmount", {}))
        paid_text = _join_text_spans(inst.get("paidStatus", {}))
        icon = inst.get("icon", {}).get("name") or ""

        last4_match = re.search(r"\b(\d{4})\b", f"{desc} {acc_text}")
        card_last4 = last4_match.group(1) if last4_match else None

        amt_c = _parse_cents(amt_text) or total_cents
        charge_date = _parse_ebay_date(paid_text) or order_date

        method = "card" if card_last4 else (icon or desc or "other")
        charges.append(
            {
                "amount_cents": amt_c,
                "card_last4": card_last4,
                "date": charge_date.isoformat() if charge_date else None,
                "method": method,
            }
        )
    return charges


def _extract_table_items(printer_friendly: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for tbl in printer_friendly.get("tables", []):
        for row in tbl.get("rows", []):
            for cell in row.get("cells", []):
                sec_tbl = cell.get("table", {})
                for tr in sec_tbl.get("rows", []):
                    tds = [_join_text_spans(c) for c in tr.get("cells", [])]
                    if len(tds) < 4:
                        continue
                    try:
                        qty = int(tds[0].strip())
                    except ValueError:
                        qty = 1
                    item_name = tds[1].strip()
                    price_c = _parse_cents(tds[3])
                    m_id = re.search(r"\((\d{10,14})\)", item_name)
                    item_id = m_id.group(1) if m_id else None
                    items.append(
                        {
                            "title": item_name,
                            "amount_cents": price_c,
                            "quantity": max(1, qty),
                            "item_id": item_id,
                        }
                    )
    return items


def _extract_shipment_items(
    shipment: dict[str, Any], subtotal_cents: int, total_cents: int
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for container in shipment.get("containers", []):
        for card in container.get("cards", []):
            for ic in card.get("itemCardContainer", {}).get("cards", []):
                title = _join_text_spans(ic.get("title", {}))
                if title:
                    items.append(
                        {
                            "title": title,
                            "amount_cents": subtotal_cents or total_cents,
                            "quantity": 1,
                            "item_id": None,
                        }
                    )
    return items


def _extract_items(
    printer_friendly: dict[str, Any],
    shipment: dict[str, Any],
    subtotal_cents: int,
    total_cents: int,
) -> list[dict[str, Any]]:
    items = _extract_table_items(printer_friendly)
    if not items:
        return _extract_shipment_items(shipment, subtotal_cents, total_cents)
    return items


def _extract_order_data(
    state: dict[str, Any], fallback_id: str, fallback_date: date | None
) -> dict[str, Any]:
    mods = state.get("modules", {})
    order_id, order_date, seller = _extract_order_info(
        mods.get("orderInfo", {}), fallback_id, fallback_date
    )
    subtotal_cents, shipping_cents, tax_cents, total_cents = _extract_payment_totals(
        mods.get("paymentInfo", {})
    )
    charges = _extract_charges(mods.get("paymentInfo", {}), order_date, total_cents)
    items = _extract_items(
        mods.get("printerFriendly", {}), mods.get("shipment", {}), subtotal_cents, total_cents
    )

    return {
        "order_id": order_id,
        "order_date": order_date.isoformat() if order_date else None,
        "total_cents": total_cents,
        "subtotal_cents": subtotal_cents,
        "tax_cents": tax_cents,
        "shipping_cents": shipping_cents,
        "seller": seller,
        "items": items,
        "charges": charges,
    }


def _parse_cents(value: str) -> int:
    clean = re.sub(r"[^\d.,]", "", value)
    if not clean or clean == ".":
        return 0
    return money.to_cents(clean)
