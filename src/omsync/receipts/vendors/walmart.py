"""Walmart orders and in-store purchases, read from the JSON files the downloader saves.

Each order is one receipt, built from its line items. Each return or refund on an order yields
a refund receipt.

Files: the sync saves one JSON file per order, <provider folder>/<year>/<order ID>.json.
Money is integer cents.
"""

import contextlib
import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from omsync.core import money
from omsync.receipts.model import (
    Charge,
    ChargeResult,
    Offset,
    Receipt,
    ReceiptItem,
    Tender,
    charge_key,
    make_receipt,
)
from omsync.receipts.vendor import BaseVendor

VENDOR_KEY = "walmart"
FOLDER = "WALMART"  # under the data directory's receipts folder
CHARGE_WINDOW_DAYS = 14
LAST_FOUR = re.compile(r"(\d{4})")


class WalmartFileError(ValueError):
    """A Walmart order file cannot be read. The message names the file, never its contents."""


def _cents_from_val(val: Any) -> int:
    if val is None:
        return 0
    if isinstance(val, (int, str)):
        cleaned = str(val).replace("$", "").replace(",", "").strip()
        if not cleaned:
            return 0
        return money.to_cents(cleaned)
    if isinstance(val, float):
        return money.to_cents(f"{val:.2f}")
    return 0


def _parse_order_date(raw_date: Any) -> date:
    if not raw_date:
        return date.today()
    try:
        return datetime.fromisoformat(str(raw_date).strip()).date()
    except ValueError:
        return date.today()


def _parse_refund_date(status_msg: str, order_date: date, delivery_date: str | None) -> date:
    """Extract refund date from status message like 'Refund issued on Jan 04'."""
    match = re.search(r"Refund issued on ([A-Za-z]{3}\s+\d{1,2})", status_msg)
    if not match:
        return order_date
    month_day = match.group(1)
    year = order_date.year
    if delivery_date:
        with contextlib.suppress(ValueError):
            year = datetime.fromisoformat(delivery_date.split()[0]).year
    try:
        parsed = datetime.strptime(f"{month_day} {year}", "%b %d %Y").date()
        if parsed < order_date:
            parsed = datetime.strptime(f"{month_day} {year + 1}", "%b %d %Y").date()
        return parsed
    except ValueError:
        return order_date


def _extract_tenders(payment_methods: list[dict[str, Any]], total_cents: int) -> tuple[Tender, ...]:
    tenders: list[Tender] = []
    for pm in payment_methods:
        desc = pm.get("description") or ""
        match = LAST_FOUR.search(desc)
        last4 = match.group(1) if match else None
        dv = pm.get("displayValues")
        pm_cents = (
            _cents_from_val(dv[0])
            if dv and len(dv) > 0
            else (total_cents if len(payment_methods) == 1 else None)
        )
        ptype = (pm.get("paymentType") or "").upper()
        kind = "card" if (last4 or "CARD" in ptype) else "other"
        tenders.append(Tender(kind=kind, last4=last4, amount_cents=pm_cents))
    return tuple(tenders)


def _extract_items(
    groups: list[dict[str, Any]], tax_cents: int, order_date: date
) -> tuple[list[ReceiptItem], list[ReceiptItem], date]:
    """Parse line items and refunded items across groups."""
    items: list[ReceiptItem] = []
    refunded_items: list[ReceiptItem] = []
    refund_date = order_date
    pos = 1
    r_pos = 1

    for g in groups:
        status = g.get("status") or {}
        msg_parts = status.get("message", {}).get("parts") or []
        msg_text = " ".join(part.get("text", "") for part in msg_parts if isinstance(part, dict))
        is_refunded = "refund" in msg_text.lower() or status.get("statusType") == "REFUNDED"

        delivery_dates = g.get("orderGroupDates") or []
        exp_delivery = next(
            (d.get("value") for d in delivery_dates if d.get("name") == "expectedDeliveryDate"),
            None,
        )
        if is_refunded:
            refund_date = _parse_refund_date(msg_text, order_date, exp_delivery)

        for it in g.get("items", []):
            prod = it.get("productInfo") or {}
            name = prod.get("name") or "Walmart item"
            qty = it.get("quantity") or 1
            price_info = it.get("priceInfo") or {}
            line_val = price_info.get("linePrice", {}).get("value")
            if line_val is None:
                line_val = price_info.get("itemPrice", {}).get("value")
            line_cents = _cents_from_val(line_val)

            item = ReceiptItem(
                position=pos,
                description=name,
                amount_cents=line_cents,
                taxable=tax_cents > 0,
                quantity=qty,
            )
            items.append(item)
            pos += 1

            if is_refunded:
                refunded_items.append(
                    ReceiptItem(
                        position=r_pos,
                        description=name,
                        amount_cents=line_cents,
                        taxable=False,
                        quantity=qty,
                    )
                )
                r_pos += 1

    return items, refunded_items, refund_date


def _resolve_purchase_total(
    subtotal_cents: int,
    tax_cents: int,
    grand_total_cents: int,
    refund_total_cents: int,
    items: list[ReceiptItem],
) -> int:
    if refund_total_cents > 0 and subtotal_cents + tax_cents > 0:
        return subtotal_cents + tax_cents
    if grand_total_cents > 0:
        return grand_total_cents
    return sum(it.amount_cents for it in items) + tax_cents


class WalmartVendor(BaseVendor):
    key = VENDOR_KEY
    folder = FOLDER

    def discover(self, provider_folder: Path) -> list[Path]:
        return sorted(
            path
            for path in provider_folder.rglob("*.json")
            if path.is_file()
            and not path.name.startswith(".")
            and not path.parent.name.startswith(".")
        )

    def provider_folder(self, receipts_dir: Path, provider_key: str) -> Path:
        return receipts_dir / self.folder / provider_key

    def receipt_file(
        self, receipts_dir: Path, provider_key: str, day: date, receipt_id: str
    ) -> Path:
        return (
            self.provider_folder(receipts_dir, provider_key) / str(day.year) / f"{receipt_id}.json"
        )

    def parse(self, path: Path, provider_key: str) -> list[Receipt]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise WalmartFileError(f"{path.name} is not a readable JSON file") from exc

        order = data.get("data", {}).get("order") or data.get("order") or data
        if not isinstance(order, dict):
            raise WalmartFileError(f"{path.name} has no order object")

        order_id = str(order.get("id") or path.stem).strip()
        if not order_id:
            raise WalmartFileError(f"{path.name} has no order id")

        order_date = _parse_order_date(order.get("orderDate"))
        pd = order.get("priceDetails") or {}
        grand_total_cents = _cents_from_val(pd.get("grandTotal", {}).get("value"))
        subtotal_cents = _cents_from_val(pd.get("subTotal", {}).get("value"))
        tax_cents = _cents_from_val(pd.get("taxTotal", {}).get("value"))
        refund_total_cents = sum(
            abs(_cents_from_val(r.get("value"))) for r in (pd.get("refund") or [])
        )

        groups = order.get("groups_2101") or order.get("groups") or []
        items, refunded_items, refund_date = _extract_items(groups, tax_cents, order_date)
        total_cents = _resolve_purchase_total(
            subtotal_cents, tax_cents, grand_total_cents, refund_total_cents, items
        )
        if total_cents == 0 and not items:
            return []

        pms = order.get("paymentMethods") or []
        tenders = _extract_tenders(pms, total_cents)
        is_order_return = (
            order.get("type") == "RETURN"
            or str(order.get("orderType", "")).upper() == "RETURN"
            or grand_total_cents < 0
        )

        main_receipt = make_receipt(
            vendor_key=self.key,
            receipt_id=order_id,
            purchased_on=order_date,
            account=provider_key,
            items=tuple(items),
            tax_cents=tax_cents,
            total_cents=abs(total_cents),
            tenders=tenders,
            is_refund=is_order_return,
        )
        receipts = [main_receipt]

        if refund_total_cents > 0 and not is_order_return:
            r_receipt = self._build_refund_receipt(
                order_id, provider_key, refund_date, refund_total_cents, refunded_items, tenders
            )
            receipts.append(r_receipt)

        return receipts

    def _build_refund_receipt(
        self,
        order_id: str,
        provider_key: str,
        refund_date: date,
        refund_total_cents: int,
        refunded_items: list[ReceiptItem],
        tenders: tuple[Tender, ...],
    ) -> Receipt:
        r_items = (
            tuple(refunded_items)
            if refunded_items
            else (
                ReceiptItem(
                    position=1,
                    description="Refund",
                    amount_cents=refund_total_cents,
                    taxable=False,
                    quantity=1,
                ),
            )
        )
        refund_last4 = tenders[0].last4 if tenders else None
        r_tenders = (
            (Tender(kind="card", last4=refund_last4, amount_cents=refund_total_cents),)
            if refund_last4
            else ()
        )
        return make_receipt(
            vendor_key=self.key,
            receipt_id=f"{order_id}-refund-{refund_date.isoformat()}-{refund_total_cents}",
            purchased_on=refund_date,
            account=provider_key,
            items=r_items,
            tax_cents=0,
            total_cents=refund_total_cents,
            tenders=r_tenders,
            is_refund=True,
            refund_of=order_id,
        )

    def charges(self, receipt: Receipt) -> ChargeResult:
        if not receipt.items:
            return ChargeResult("insufficient", (), "the receipt lists no items")
        if any(item.amount_cents < 0 for item in receipt.items):
            return ChargeResult("insufficient", (), "an item has a negative amount")

        cards = [
            (index, tender) for index, tender in enumerate(receipt.tenders) if tender.kind == "card"
        ]
        if not cards:
            return ChargeResult("insufficient", (), "paid without a card")

        window_start = receipt.purchased_on - timedelta(days=1)
        window_end = receipt.purchased_on + timedelta(days=CHARGE_WINDOW_DAYS)

        charges: list[Charge] = []
        for index, tender in cards:
            if tender.amount_cents is None:
                return ChargeResult("insufficient", (), "a card tender has no amount")
            if tender.amount_cents == 0:
                continue
            paid = abs(tender.amount_cents)
            charges.append(
                Charge(
                    receipt_key=charge_key(self.key, receipt.receipt_id, index),
                    amount_cents=paid if receipt.is_refund else -paid,
                    window_start=window_start,
                    window_end=window_end,
                    card_last4=tender.last4,
                    item_positions=None,
                    observed=True,
                )
            )

        if not charges:
            return ChargeResult("insufficient", (), "the card tenders add up to nothing")

        if len(charges) == 1:
            others = [t for t in receipt.tenders if t.kind != "card" and t.amount_cents]
            if others:
                offsets = tuple(
                    Offset(description=t.kind, amount_cents=t.amount_cents or 0) for t in others
                )
                charges = [replace(charges[0], offsets=offsets)]

        return ChargeResult("observed", tuple(charges))

    def receipt_url(self, receipt_id: str) -> str | None:
        base_id = receipt_id.split("-refund-")[0]
        return f"https://www.walmart.com/orders/{base_id}"

    def receipt_label(self, receipt_id: str) -> str:
        if "-refund-" in receipt_id:
            return f"Walmart refund {receipt_id}"
        return f"Walmart order {receipt_id}"
