"""Route tests for the transactions register: the page, cell edits, the split editor, and undo.

The app runs on a temporary data directory with a fake vault. Rows are synthetic and seeded
directly, so no test touches the real data directory.
"""

import re
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from register_support import revision_in
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from web_support import sign_in

from omsync.core.batch_models import Batch
from omsync.ledger import register
from omsync.ledger.links import Link, LinkKind
from omsync.ledger.models import (
    Account,
    AccountType,
    Category,
    CategoryOrigin,
    Institution,
    Split,
    Transaction,
)
from omsync.ledger.tags import get_tag_by_name


def seed(app: FastAPI, *, rows: int = 0) -> dict[str, int]:
    """Two accounts, two categories and one hidden category, and three transactions in the
    checking account plus one in the card. `rows` adds that many more in checking."""
    with Session(app.state.engine) as db:
        bank = Institution(key="bank-a", name="Bank A")
        db.add(bank)
        db.flush()
        checking = Account(
            institution_id=bank.id,
            name="Everyday Checking",
            type=AccountType.checking,
            last4="1234",
        )
        card = Account(
            institution_id=bank.id,
            name="Travel Card",
            type=AccountType.credit_card,
            last4="4321",
        )
        groceries = Category(name="Groceries")
        dining = Category(name="Dining")
        secret = Category(name="Secret", hidden=True)
        db.add_all([checking, card, groceries, dining, secret])
        db.flush()

        def txn(account: Account, on: date, cents: int, payee: str) -> Transaction:
            row = Transaction(
                account_id=account.id,
                posted_on=on,
                amount_cents=cents,
                payee=payee,
                payee_raw=payee.upper(),
                memo=None,
                category_origin=CategoryOrigin.none,
            )
            db.add(row)
            return row

        maple = txn(checking, date(2026, 10, 3), -4250, "Maple Grocery")
        city = txn(checking, date(2026, 10, 2), -9900, "City Power Co")
        oldest = txn(checking, date(2026, 9, 30), -1200, "Bluebird Coffee")
        flight = txn(card, date(2026, 10, 1), -45000, "Blue Sky Travel")
        for n in range(rows):
            txn(checking, date(2025, 1, 1) + timedelta(days=n), -100 - n, f"Synthetic {n:03d}")
        db.commit()
        return {
            "checking": checking.id,
            "card": card.id,
            "groceries": groceries.id,
            "dining": dining.id,
            "secret": secret.id,
            "maple": maple.id,
            "city": city.id,
            "oldest": oldest.id,
            "flight": flight.id,
        }


def count_batches(app: FastAPI) -> int:
    with Session(app.state.engine) as db:
        return int(db.scalar(select(func.count(Batch.id))) or 0)


def stored(app: FastAPI, txn_id: int) -> Transaction:
    with Session(app.state.engine) as db:
        row = db.get(Transaction, txn_id)
        assert row is not None
        db.expunge(row)
        return row


def split_count(app: FastAPI, txn_id: int) -> int:
    with Session(app.state.engine) as db:
        return int(
            db.scalar(select(func.count(Split.id)).where(Split.transaction_id == txn_id)) or 0
        )


def version_of(app: FastAPI, txn_id: int, field_name: str) -> str:
    """The token a cell of this field, rendered right now, would carry."""
    with Session(app.state.engine) as db:
        return register.field_version(register.get_transaction_row(db, txn_id), field_name)


def row_version_of(app: FastAPI, txn_id: int) -> str:
    """The token a split editor opened on this transaction right now would carry."""
    with Session(app.state.engine) as db:
        return register.row_version(register.get_transaction_row(db, txn_id))


def batch_id_in(body: str) -> int:
    match = re.search(r"/batches/(\d+)/undo", body)
    assert match, "the toast has no Undo button"
    return int(match.group(1))


@pytest.fixture
def client_signed_in(client: TestClient) -> TestClient:
    sign_in(client)
    return client


# Access


# The page


def payees(page: str) -> list[str]:
    return re.findall(r'data-field="payee" data-txn-id="\d+"><span[^>]*>([^<]*)</span></td>', page)


# Cell display and editing


def test_a_save_that_races_another_change_is_a_409_and_writes_nothing(
    client_signed_in: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = seed(app)
    real_make_preview = register.make_preview

    def race_after_preview(session, kind, changes, summary=None):  # type: ignore[no-untyped-def]
        preview = real_make_preview(session, kind, changes, summary)
        session.execute(text("INSERT OR IGNORE INTO data_revision (id, value) VALUES (1, 0)"))
        session.execute(text("UPDATE data_revision SET value = value + 1"))
        return preview

    monkeypatch.setattr(register, "make_preview", race_after_preview)

    response = client_signed_in.post(
        f"/transactions/{ids['maple']}/cell/category",
        data={
            "value": str(ids["groceries"]),
            "version": version_of(app, ids["maple"], "category"),
        },
    )

    assert response.status_code == 409
    assert "This changed elsewhere, reload." in response.text
    assert "<select" in response.text
    assert stored(app, ids["maple"]).category_id is None
    assert count_batches(app) == 0


def test_the_same_key_twice_applies_once(client_signed_in: TestClient, app: FastAPI) -> None:
    ids = seed(app)
    data = {
        "value": str(ids["groceries"]),
        "idempotency_key": "double-submit",
        "version": version_of(app, ids["maple"], "category"),
    }

    first = client_signed_in.post(f"/transactions/{ids['maple']}/cell/category", data=data)
    second = client_signed_in.post(f"/transactions/{ids['maple']}/cell/category", data=data)

    assert first.status_code == second.status_code == 200
    assert batch_id_in(first.text) == batch_id_in(second.text)
    assert count_batches(app) == 1


# Undo


# Split editor


def test_split_lines_must_add_up_to_the_transaction(
    client_signed_in: TestClient, app: FastAPI
) -> None:
    ids = seed(app)

    response = client_signed_in.post(
        f"/transactions/{ids['maple']}/split",
        data={
            "amount": ["-20.00", "-20.00"],
            "category": [str(ids["groceries"]), str(ids["dining"])],
            "memo": ["", ""],
            "version": row_version_of(app, ids["maple"]),
        },
    )

    assert response.status_code == 422
    assert "Lines must add up to -$42.50 exactly." in response.text
    assert "split-editor" in response.text
    assert split_count(app, ids["maple"]) == 0
    assert count_batches(app) == 0


def test_tag_settings_and_register_tags(client_signed_in: TestClient, app: FastAPI) -> None:
    ids = seed(app)
    maple, city = ids["maple"], ids["city"]

    def tags_of(txn_id: int) -> list[str]:
        with Session(app.state.engine) as db:
            return [t.name for t in register.get_transaction_row(db, txn_id).tags]

    def named(name: str) -> int:
        with Session(app.state.engine) as db:
            tag = get_tag_by_name(db, name)
            assert tag is not None
            return tag.id

    # Settings: create, a clash, rename, and the empty page first.
    page = client_signed_in.get("/settings/tags")
    assert page.status_code == 200
    assert "No tags yet." in page.text
    assert "description" not in client_signed_in.get("/settings/tags/new").text.lower()
    created = client_signed_in.post("/settings/tags", data={"name": "Tax 2026", "color": "#e99537"})
    assert created.status_code == 200
    assert "Created tag Tax 2026" in created.text
    assert client_signed_in.post("/settings/tags", data={"name": "tax 2026"}).status_code == 422
    tax = named("Tax 2026")
    client_signed_in.post(f"/settings/tags/{tax}", data={"name": "Tax", "color": "#4da568"})
    tax = named("Tax")
    assert "Tax" in client_signed_in.get("/settings/tags").text

    # Register: the popover lists the tag, toggling puts it on and takes it off, and creating a tag
    # from the popover is one batch that one undo reverses.
    popover = client_signed_in.get(f"/transactions/{maple}/tags/popover")
    assert "Tax" in popover.text and "New tag" in popover.text
    on = client_signed_in.post(f"/transactions/{maple}/tags/toggle/{tax}")
    assert on.status_code == 200 and tags_of(maple) == ["Tax"]
    assert "Tags" in client_signed_in.get("/transactions").text
    gone = client_signed_in.post(f"/transactions/{maple}/tags/toggle/9999")
    assert "/batches/0/undo" not in gone.text and "no longer exists" in gone.text
    made = client_signed_in.post(
        f"/transactions/{maple}/tags/create", data={"name": "Freelance", "color": "#6471eb"}
    )
    assert tags_of(maple) == ["Freelance", "Tax"]
    undo = client_signed_in.post(f"/batches/{batch_id_in(made.text)}/undo")
    assert undo.status_code == 200
    assert tags_of(maple) == ["Tax"]
    assert get_tag_by_name_of(app, "Freelance") is None

    # Memo has one save path, and a memo that is as it was shows no toast.
    assert "Add a memo…" in client_signed_in.get(f"/transactions/{maple}/memo/popover").text
    saved = client_signed_in.post(f"/transactions/{maple}/memo", data={"memo": "Audited"})
    assert "/undo" in saved.text
    same = client_signed_in.post(f"/transactions/{maple}/memo", data={"memo": "Audited"})
    assert "/undo" not in same.text

    # Detail page, with the shared picker.
    detail = client_signed_in.get(f"/transactions/{maple}/detail")
    assert detail.status_code == 200 and "Manage tags" in detail.text
    off = client_signed_in.post(f"/transactions/{maple}/tags/toggle/{tax}?from_detail=1")
    assert off.status_code == 200 and tags_of(maple) == []

    # Split lines keep their tags when the split is saved again, and undo restores them.
    split = {
        "amount": ["-20.00", "-22.50"],
        "category": [str(ids["groceries"]), str(ids["dining"])],
        "memo": ["", ""],
        "tag": ["-", str(tax), "-"],
        "version": row_version_of(app, maple),
    }
    first = client_signed_in.post(f"/transactions/{maple}/split", data=split)
    assert first.status_code == 200 and "Tax" in first.text
    resplit = {
        **split,
        "amount": ["-10.00", "-32.50"],
        "version": row_version_of(app, maple),
    }
    again = client_signed_in.post(f"/transactions/{maple}/split", data=resplit)
    assert again.status_code == 200
    assert split_tag_names(app, maple) == [["Tax"], []]
    client_signed_in.post(f"/batches/{batch_id_in(again.text)}/undo")
    assert split_tag_names(app, maple) == [["Tax"], []]
    split_id = split_ids(app, maple)[1]
    line = client_signed_in.post(f"/transactions/{maple}/splits/{split_id}/tags/toggle/{tax}")
    assert line.status_code == 200
    assert split_tag_names(app, maple) == [["Tax"], ["Tax"]]
    new_line_tag = client_signed_in.post(
        "/tags/split-picker", data={"picker": "x", "new_name": "Trip", "new_color_x": "#db5a54"}
    )
    assert new_line_tag.status_code == 200 and "Trip" in new_line_tag.text

    # Bulk add, then reports limited to the tag.
    bulk = {"bulk_action": "add_tag", "value": str(tax), "ids": [city]}
    preview = client_signed_in.post("/transactions/bulk/preview", data=bulk)
    assert preview.status_code == 200
    applied = client_signed_in.post(
        "/transactions/bulk/apply", data={**bulk, "revision": revision_in(preview.text)}
    )
    assert applied.status_code == 200 and tags_of(city) == ["Tax"]
    everything = client_signed_in.get("/reports?period=custom&start=2026-09-01&end=2026-10-31")
    limited = client_signed_in.get(
        f"/reports?period=custom&start=2026-09-01&end=2026-10-31&tag={tax}"
    )
    assert limited.status_code == 200
    assert "Bluebird Coffee" not in limited.text
    assert limited.text != everything.text
    assert 'name="tag"' in limited.text

    # Delete: a used tag moves its transactions to another tag, skipping ones that have it.
    other = named("Trip")
    client_signed_in.post(f"/transactions/{city}/tags/toggle/{other}")
    assert "Move its transactions to" in client_signed_in.get(f"/settings/tags/{tax}/delete").text
    moved = client_signed_in.post(
        f"/settings/tags/{tax}/delete", data={"replacement_tag_id": str(other)}
    )
    assert moved.status_code == 200
    assert tags_of(city) == ["Trip"]
    assert split_tag_names(app, maple) == [["Trip"], ["Trip"]]
    assert client_signed_in.post(f"/batches/{batch_id_in(moved.text)}/undo").status_code == 200
    assert named("Tax") == tax and tags_of(city) == ["Tax", "Trip"]

    # Delete all, then undo.
    wiped = client_signed_in.post("/settings/tags/delete-all")
    assert wiped.status_code == 200
    assert tags_of(city) == [] and split_tag_names(app, maple) == [[], []]
    client_signed_in.post(f"/batches/{batch_id_in(wiped.text)}/undo")
    assert tags_of(city) == ["Tax", "Trip"]


def get_tag_by_name_of(app: FastAPI, name: str) -> object:
    with Session(app.state.engine) as db:
        return get_tag_by_name(db, name)


def split_ids(app: FastAPI, txn_id: int) -> list[int]:
    with Session(app.state.engine) as db:
        return [s.id for s in register.get_transaction_row(db, txn_id).splits]


def split_tag_names(app: FastAPI, txn_id: int) -> list[list[str]]:
    with Session(app.state.engine) as db:
        return [[t.name for t in s.tags] for s in register.get_transaction_row(db, txn_id).splits]


def test_receipt_links_open_directly_while_transaction_and_multiple_links_use_menu(
    client_signed_in: TestClient, app: FastAPI
) -> None:
    ids = seed(app)
    with Session(app.state.engine) as db:
        db.add_all(
            [
                Link(
                    kind=LinkKind.receipt.value,
                    amount_cents=4250,
                    from_transaction_id=ids["maple"],
                    receipt_key="costco:R123:0",
                    evidence={},
                ),
                Link(
                    kind=LinkKind.receipt.value,
                    amount_cents=9900,
                    from_transaction_id=ids["city"],
                    receipt_key="amazon:113-42:0",
                    evidence={},
                ),
                Link(
                    kind=LinkKind.refund.value,
                    amount_cents=1200,
                    from_transaction_id=ids["city"],
                    to_transaction_id=ids["oldest"],
                    evidence={},
                ),
            ]
        )
        db.commit()

    page = client_signed_in.get("/transactions")
    assert page.status_code == 200
    assert f"txn-links-{ids['maple']}" not in page.text
    assert 'href="/receipts/costco/R123" class="link-btn"' in page.text
    assert 'target="_blank" rel="noopener noreferrer"' in page.text
    assert f"txn-links-{ids['oldest']}" in page.text
    assert f"txn-links-{ids['city']}" in page.text
    assert 'href="/receipts/amazon/113-42"' in page.text


def test_last_page_and_date_sort_toggle_and_persist(
    client_signed_in: TestClient, app: FastAPI
) -> None:
    seed(app, rows=55)

    first = client_signed_in.get("/transactions")
    assert first.status_code == 200
    assert 'aria-sort="descending"' in first.text
    assert "chevron-last" in first.text
    assert "Maple Grocery" in first.text and "Synthetic 000" not in first.text

    last = client_signed_in.get("/transactions/table?page=2&focus=last")
    assert last.status_code == 200
    assert "2 / 2" in last.text
    assert "Synthetic 000" in last.text

    asc = client_signed_in.get("/transactions?sort=date_asc")
    assert asc.status_code == 200
    assert 'aria-sort="ascending"' in asc.text
    assert 'name="sort" value="date_asc"' in asc.text
    assert "Synthetic 000" in asc.text and "Maple Grocery" not in asc.text

    asc_last = client_signed_in.get("/transactions/table?page=2&sort=date_asc&focus=last")
    assert asc_last.status_code == 200
    assert "2 / 2" in asc_last.text
    assert 'aria-sort="ascending"' in asc_last.text
    assert "Maple Grocery" in asc_last.text


def test_date_between_filter_shows_both_inputs_and_filters_inclusively(
    client_signed_in: TestClient, app: FastAPI
) -> None:
    seed(app)

    form = client_signed_in.get("/transactions/filter/form?field=date")
    assert form.status_code == 200
    assert 'data-ops="is after before"' not in form.text
    assert 'name="value"' in form.text and 'name="value_to"' in form.text

    applied = client_signed_in.post(
        "/transactions/filter/add",
        data={"field": "date", "op": "between", "value": "2026-10-01", "value_to": "2026-10-02"},
    )
    assert applied.status_code == 200
    assert "Date between" in applied.text
    assert "City Power Co" in applied.text and "Blue Sky Travel" in applied.text
    assert "Maple Grocery" not in applied.text and "Bluebird Coffee" not in applied.text

    match = re.search(r'name="f" value="([^"]+)"', applied.text)
    assert match is not None
    reopened = client_signed_in.get(f"/transactions/filter/form?index=0&f={match.group(1)}")
    assert reopened.status_code == 200
    assert 'value="2026-10-01"' in reopened.text
    assert 'value="2026-10-02"' in reopened.text
