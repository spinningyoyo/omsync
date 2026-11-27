"""Fake Costco and Amazon receipts for the demo, matched to the demo's own card charges.

Each receipt is written in the exact JSON shape its vendor's downloader saves, with integer cents
adding up to the card charge to the cent, then matched and applied through `omsync.receipts.service`
like real data.
"""

import json
import re
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.demo.statements import Line
from omsync.ledger.models import ProviderKind
from omsync.receipts.service import apply_report, run_vendor
from omsync.sync.providers import create_provider

COSTCO_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("KS ORG OLIVE OIL", "Groceries"),
    ("ORGANIC BANANAS", "Groceries"),
    ("KS PAPER TOWELS", "Home"),
)

AMAZON_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("USB-C CHARGING CABLE", "Shopping"),
    ("CERAMIC POUR-OVER DRIPPER", "Home"),
)

RECEIPT_COUNT = 3
_JSON_NUMBER = re.compile(r'"@(-?\d+\.\d\d)"')


def setup_providers(session: Session) -> None:
    """Register the demo's Costco and Amazon receipt providers."""
    for key, name in (("costco", "Costco"), ("amazon", "Amazon")):
        create_provider(
            session,
            provider_key=key,
            provider_name=name,
            connector_key=key,
            vault_item_id=None,
            kind=ProviderKind.receipt,
            idempotency_key=f"demo:provider:receipt:{key}",
        )


def write_receipts(receipts_dir: Path, lines: list[Line]) -> None:
    """Write fake Costco and Amazon receipts matching the most recent demo card charges."""
    for index, line in enumerate(_recent_costco(lines), start=1):
        _write_costco(receipts_dir, line, index)
    for index, line in enumerate(_recent_amazon(lines), start=1):
        _write_amazon(receipts_dir, line, index)


def apply_all(session: Session, settings: Settings, today: date) -> None:
    """Match and apply the demo's Costco and Amazon receipts through the real receipts service."""
    for vendor_key in ("costco", "amazon"):
        report = run_vendor(session, settings, vendor_key, today=today)
        if report.to_apply == 0:
            raise RuntimeError(f"Demo {vendor_key} receipts did not match any charges")
        apply_report(session, report, idempotency_key=f"demo:receipts:{vendor_key}")


def _recent_costco(lines: list[Line]) -> list[Line]:
    charges = [
        line for line in lines if line.description == "COSTCO WHSE #0144" and line.amount_cents < 0
    ]
    return charges[-RECEIPT_COUNT:]


def _recent_amazon(lines: list[Line]) -> list[Line]:
    refunded = {
        line.description
        for line in lines
        if line.description.startswith("AMZN Mktp US*") and line.amount_cents > 0
    }
    charges = [
        line
        for line in lines
        if line.description.startswith("AMZN Mktp US*")
        and line.amount_cents < 0
        and line.description not in refunded
    ]
    return charges[-RECEIPT_COUNT:]


def _write_costco(receipts_dir: Path, line: Line, index: int) -> None:
    total = abs(line.amount_cents)
    tax = (total * 3) // 100
    subtotal = total - tax
    food = (subtotal * 65) // 100
    home = subtotal - food
    olive_oil = food // 2
    bananas = food - olive_oil
    barcode = f"2026144{line.day:%Y%m%d}{index:02d}"

    payload = {
        "transactionBarcode": barcode,
        "transactionDate": line.day.isoformat(),
        "transactionType": "Sales",
        "itemArray": [
            _costco_item(104210, "KS ORG OLIVE OIL", olive_oil, "N", "E"),
            _costco_item(108932, "ORGANIC BANANAS", bananas, "N", "E"),
            _costco_item(512340, "KS PAPER TOWELS", home, "Y", None),
        ],
        "taxes": f"@{_dollars(tax)}",
        "total": f"@{_dollars(total)}",
        "tenderArray": [
            {
                "tenderDescription": "VISA",
                "displayAccountNumber": line.account.last4,
                "amountTender": f"@{_dollars(total)}",
            }
        ],
    }
    folder = receipts_dir / "COSTCO" / "costco" / str(line.day.year)
    folder.mkdir(parents=True, exist_ok=True)
    text = _JSON_NUMBER.sub(r"\1", json.dumps(payload, indent=2))
    (folder / f"{barcode}.json").write_text(text + "\n", encoding="utf-8")


def _costco_item(
    number: int, description: str, cents: int, tax_flag: str, code: str | None
) -> dict[str, object]:
    return {
        "itemNumber": number,
        "itemDescription01": description,
        "itemDescription02": None,
        "frenchItemDescription1": None,
        "itemIdentifier": code,
        "amount": f"@{_dollars(cents)}",
        "taxFlag": tax_flag,
        "unit": 1,
    }


def _write_amazon(receipts_dir: Path, line: Line, index: int) -> None:
    total = abs(line.amount_cents)
    tax = (total * 8) // 100
    order_id = f"113-{1000000 + index}-{line.day:%m%d}{total % 1000:03d}"
    if total >= 2500:
        dripper = (total * 60) // 100
        cable = total - dripper
        items = [
            {"title": "Ceramic Pour-Over Dripper", "amount_cents": dripper, "quantity": 1},
            {"title": "USB-C Charging Cable", "amount_cents": cable, "quantity": 1},
        ]
    else:
        items = [{"title": "USB-C Charging Cable", "amount_cents": total, "quantity": 1}]

    payload = {
        "order_id": order_id,
        "order_date": line.day.isoformat(),
        "total_cents": total,
        "tax_cents": tax,
        "items": items,
        "charges": [
            {
                "amount_cents": total,
                "card_last4": line.account.last4,
                "date": line.day.isoformat(),
            }
        ],
        "refunds": [],
    }
    folder = receipts_dir / "AMAZON" / "amazon" / str(line.day.year)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{order_id}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"
