"""Costco receipts applied to imported charges, end to end through the CLI.

The charges come in as a synthetic card statement. The receipts are synthetic files in the shape
the Costco download saves, written under <data_dir>/receipts/COSTCO. Each case is one way Costco
prints a line that is not a plain product, and asserts the category each charge ends with."""

import json
import re
from datetime import date
from pathlib import Path

import pytest
from ledgerkit import CARD, Ledger, Row, web_client

PAYEE = "COSTCO WHSE #0001 ANYTOWN CA"
KEYWORDS = (
    '{"Household": ["SOAP", "LAMP"], "Groceries": ["BREAD"], '
    '"Rewards": ["2% REBATE"], "Transfer": ["SHOP CARD"]}'
)


def money(cents: int) -> str:
    """Dollars as a JSON number, written from integer cents."""
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def line(
    number: int,
    description: str,
    cents: int | None,
    flag: str | None = None,
    link=None,
    code: str | None = None,
) -> tuple[dict, int]:
    """One itemArray line and its cents. `link` is what Costco writes in frenchItemDescription1,
    and `code` its itemIdentifier, E for food. An amount marked "@" is written as a JSON number."""
    raw = {
        "itemNumber": number,
        "itemDescription01": description,
        "itemDescription02": None,
        "frenchItemDescription1": link,
        "itemIdentifier": code,
        "amount": None if cents is None else f"@{money(cents)}",
        "taxFlag": flag,
        "unit": 1,
    }
    return raw, cents or 0


def write_receipt(
    data_dir: Path,
    barcode: str,
    day: date,
    lines: list[tuple[dict, int]],
    tax: int,
    card: str = "1111",
    others: tuple[tuple[str, int], ...] = (),
) -> None:
    """A receipt paid by the card, and by `others`, such as ("2% Rebate", 1000), for the rest."""
    total = sum(cents for _raw, cents in lines) + tax
    tenders = [
        {"tenderDescription": name, "displayAccountNumber": "0000", "amountTender": f"@{money(c)}"}
        for name, c in others
    ]
    tenders.append(
        {
            "tenderDescription": "VISA",
            "displayAccountNumber": card,
            "amountTender": f"@{money(total - sum(c for _name, c in others))}",
        }
    )
    receipt = {
        "transactionBarcode": barcode,
        "transactionDate": day.isoformat(),
        "transactionType": "Refund" if total < 0 else "Sales",
        "itemArray": [raw for raw, _cents in lines],
        "taxes": f"@{money(tax)}",
        "total": f"@{money(total)}",
        "tenderArray": tenders,
    }
    folder = data_dir / "receipts" / "COSTCO" / str(day.year) / f"Q{(day.month - 1) // 3 + 1}"
    folder.mkdir(parents=True, exist_ok=True)
    text = re.sub(r'"@(-?\d+\.\d\d)"', r"\1", json.dumps(receipt))
    (folder / f"{barcode}.json").write_text(text, encoding="utf-8")


def ready(ledger: Ledger, tmp_path: Path, charges: list[tuple[date, int]]) -> None:
    ledger.add_account("Costco Card", CARD, last4="1111")
    rows = [Row(day, cents, PAYEE, f"fit-{i}") for i, (day, cents) in enumerate(charges)]
    ledger.statement("Costco Card", rows)
    ledger.import_all()
    keywords = tmp_path / "costco-keywords.json"
    keywords.write_text(KEYWORDS, encoding="utf-8")
    ledger.cli("keywords", "import", keywords, "--list", "costco", "--apply")


def categories(ledger: Ledger, cents: int) -> list[tuple[str | None, int]]:
    """The categories of the charge of this amount: its splits, or its own category whole."""
    [(txn_id, category)] = ledger.query(
        'SELECT t.id, c.name FROM "transaction" t LEFT JOIN category c ON c.id = t.category_id '
        "WHERE t.amount_cents = ?",
        (cents,),
    )
    splits = ledger.query(
        "SELECT c.name, s.amount_cents FROM split s JOIN category c ON c.id = s.category_id "
        "WHERE s.transaction_id = ? ORDER BY s.position",
        (txn_id,),
    )
    return splits or [(category, cents)]


D1, D2 = date(2026, 3, 2), date(2026, 3, 5)

CASES = {
    # A discount on a taxed item has no tax flag of its own. It folds into the item, and the tax
    # goes to the taxed item only.
    "discount without a tax flag": (
        [("R1", D1, [line(101, "SOAP", 1000, "Y"), line(9001, "/101", -200, link="/101"),
                     line(102, "BREAD", 500, "N")], 64)],
        [(D1, -1364)],
        {-1364: [("Household", -864), ("Groceries", -500)]},
    ),
    # Two lines of one item that cancel are a void.
    "void pair": (
        [("R2", D1, [line(103, "LAMP", 2000, "Y"), line(103, "LAMP", -2000, "Y"),
                     line(102, "BREAD", 500, "N")], 0)],
        [(D1, -500)],
        {-500: [("Groceries", -500)]},
    ),
    # A return takes back the coupon the purchase had, on a line that names no item ("/0").
    "coupon taken back on a return": (
        [("R3", D1, [line(101, "SOAP", -1000, "Y"), line(9002, "CPN/ E/RETUN", 200, link="/0")],
          -64)],
        [(D1, 864)],
        {864: [("Household", 864)]},
    ),
    # A later credit names only the item number of an earlier purchase.
    "price adjustment of an earlier purchase": (
        [("R4", D1, [line(102, "BREAD", 500, "N")], 0),
         ("R5", D2, [line(9003, "/ 102", -150, link="/0")], 0)],
        [(D1, -500), (D2, 150)],
        {-500: [("Groceries", -500)], 150: [("Groceries", 150)]},
    ),
    # A discount line can come without an amount.
    "discount without an amount": (
        [("R6", D1, [line(101, "SOAP", 1000, "Y"), line(9004, "/SOAP", None)], 80)],
        [(D1, -1080)],
        {-1080: [("Household", -1080)]},
    ),
    # A reward paid most of a purchase. The items keep their full price, and the reward is a line
    # of its own, so the lines add up to what the card paid.
    "reward paid part of a purchase": (
        [("R11", D1, [line(101, "SOAP", 13000, "N")], 0, (("2% Rebate", 10210),))],
        [(D1, -2790)],
        {-2790: [("Household", -13000), ("Rewards", 10210)]},
    ),
    # A return went partly back onto a shop card.
    "return refunded partly onto a shop card": (
        [("R12", D1, [line(101, "SOAP", -2705, "N")], 0, (("Shop Card", -2000),))],
        [(D1, 705)],
        {705: [("Household", 2705), ("Transfer", -2000)]},
    ),
}  # fmt: skip


@pytest.mark.parametrize(("receipts", "charges", "expected"), CASES.values(), ids=CASES.keys())
def test_lines_that_are_not_products_still_explain_the_charge(
    ledger: Ledger, tmp_path: Path, receipts, charges, expected
) -> None:
    ready(ledger, tmp_path, charges)
    for barcode, day, lines, tax, *others in receipts:
        write_receipt(ledger.data_dir, barcode, day, lines, tax, others=others[0] if others else ())
    total = ledger.total_cents()

    applied = ledger.cli("receipts", "run", "costco", "--apply")

    assert f"Applied {len(charges)} charges as batch" in applied.output, applied.output
    for cents, parts in expected.items():
        assert categories(ledger, cents) == parts
    assert ledger.total_cents() == total


def test_a_payment_with_no_keyword_waits_and_is_named(ledger: Ledger, tmp_path: Path) -> None:
    ready(ledger, tmp_path, [(D1, -500)])
    write_receipt(
        ledger.data_dir,
        "R13",
        D1,
        [line(101, "SOAP", 3000, "N")],
        0,
        others=(("Citi Rebate", 2500),),
    )

    waiting = ledger.cli("receipts", "run", "costco", "--apply")

    assert "1 receipt item(s) need a category first: CITI REBATE" in waiting.output, waiting.output
    assert categories(ledger, -500) == [(None, -500)]


def test_food_with_no_keyword_takes_the_food_category_and_a_keyword_still_wins(
    ledger: Ledger, tmp_path: Path
) -> None:
    ready(ledger, tmp_path, [(D1, -2000)])
    food = [
        line(104, "PEARS", 700, "N", code="E"),
        line(101, "SOAP", 1000, "N", code="E"),
        line(103, "LAMP", 300, "N"),
    ]
    write_receipt(ledger.data_dir, "R10", D1, food, 0)
    waiting = ledger.cli("receipts", "run", "costco", "--apply")
    assert "1 receipt item(s) need a category first: PEARS" in waiting.output, waiting.output

    [(groceries,)] = ledger.query("SELECT id FROM category WHERE name = 'Groceries'")
    saved = web_client(ledger.data_dir).post(
        "/categories/keywords/costco-food", data={"category_id": str(groceries)}
    )
    assert saved.status_code == 200, saved.text
    applied = ledger.cli("receipts", "run", "costco", "--apply")

    assert "Applied 1 charges as batch" in applied.output, applied.output
    assert categories(ledger, -2000) == [("Groceries", -700), ("Household", -1300)]


def test_a_number_only_receipts_show_matches_its_account_and_sync_keeps_the_bank_number(
    ledger: Ledger, tmp_path: Path
) -> None:
    ready(ledger, tmp_path, [(D1, -1080)])
    write_receipt(ledger.data_dir, "R9", D1, [line(101, "SOAP", 1000, "Y")], 80, card="0721")
    assert "paid elsewhere: 1" in ledger.cli("receipts", "run", "costco").output

    ledger.cli(
        "accounts", "alias", "--account-id", "1", "--institution", "bank",
        "--statement-account", "0721", "--receipts-only",
    )  # fmt: skip
    applied = ledger.cli("receipts", "run", "costco", "--apply")

    assert "Applied 1 charges as batch" in applied.output, applied.output
    assert categories(ledger, -1080) == [("Household", -1080)]
    listed = ledger.cli("accounts", "list").output
    assert "••1111" in listed
    assert "0721" not in listed


def test_a_return_without_card_digits_waits_and_an_even_exchange_is_no_receipt(
    ledger: Ledger, tmp_path: Path
) -> None:
    ready(ledger, tmp_path, [(D1, 1080)])
    write_receipt(ledger.data_dir, "R7", D1, [line(101, "SOAP", -1000, "Y")], -80, card="0000")
    exchange = [line(103, "LAMP", -2000, "Y"), line(0, "Dept Ring || 21", 2000, "Y")]
    write_receipt(ledger.data_dir, "R8", D2, exchange, 0, card="0000")

    applied = ledger.cli("receipts", "run", "costco", "--apply")

    assert "Read 2 files and 1 receipts for costco." in applied.output
    assert "unknown payer: 1" in applied.output
    assert "Nothing to apply." in applied.output
    assert categories(ledger, 1080) == [(None, 1080)]
