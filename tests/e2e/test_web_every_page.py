"""Every page of the app opens on the demo's year of money, signed in.

The demo has accounts, contacts with balances, goals, budgets, bills and receipts, so a page that
fails on data it has not met before fails here first."""

import sqlite3
from datetime import date
from pathlib import Path

import keyring
import pytest
from ledgerkit import web_client

from omsync.demo.build import build_demo

PAGES = (
    "/",
    "/reports",
    "/transactions",
    "/recurring",
    "/recurring?view=calendar",
    "/recurring?view=all",
    "/reconcile",
    "/plan",
    "/plan/budget",
    "/plan/goals",
    "/settings/categories",
    "/settings/guides",
    "/settings/providers",
    "/settings/contacts",
    "/settings/rental",
    "/settings/about",
)


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("demo") / "data"
    build_demo(data, today=date(2026, 11, 14))
    return data


def ids(data: Path, table: str) -> list[int]:
    with sqlite3.connect(data / "omsync.db") as connection:
        return [row[0] for row in connection.execute(f'SELECT id FROM "{table}" ORDER BY id')]


def test_every_page_opens(demo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keyring, "get_password", lambda *args: None)  # an empty keychain
    client = web_client(demo)
    pages = [
        *PAGES,
        *(f"/settings/contacts/{contact}" for contact in ids(demo, "contact")),
        *(f"/plan/goals/{goal}" for goal in ids(demo, "goal")),
        *(f"/accounts/{account}" for account in ids(demo, "account")),
        f"/transactions/{ids(demo, 'transaction')[-1]}/detail",
    ]

    failed = [(page, status) for page in pages if (status := client.get(page).status_code) != 200]

    assert failed == []
