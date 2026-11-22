"""Browser journeys on the register and a transaction's page."""

import re

import pytest
from browser_support import Live, expect_on_top, reload, settle, undo, unique
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

PAYEE = "AMC METREON 16"


def _row(user: Page):
    return user.get_by_role("row", name=re.compile(f"^Select {PAYEE} ")).first


def test_search_narrows_the_register(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/transactions")
    search = user.get_by_role("searchbox", name="Search transactions")
    search.fill("METREON")
    settle(user)
    rows = user.get_by_role("row", name=re.compile("^Select (?!all)"))
    expect(rows.first).to_be_visible()
    for text in rows.all_inner_texts():
        assert "METREON" in text.upper()


def test_a_rows_category_is_changed_and_undone(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/transactions?q=METREON")
    settle(user)
    _row(user).get_by_label("Category").select_option(label="Gifts")
    settle(user)
    reload(user)
    expect(_row(user).get_by_label("Category").locator("option:checked")).to_have_text("Gifts")

    _row(user).get_by_label("Category").select_option(label="Dining")
    undo(user)
    reload(user)
    expect(_row(user).get_by_label("Category").locator("option:checked")).to_have_text("Gifts")


def test_the_actions_menu_opens_fully_and_leads_to_the_transaction(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/transactions?q=METREON")
    settle(user)
    user.get_by_role("button", name=f"Actions for {PAYEE}").first.click()
    item = user.get_by_role("menuitem", name="Counterparty").first
    expect_on_top(item)
    item.click()
    expect(user).to_have_url(re.compile("/transactions/\\d+/detail"))
    expect(user.get_by_role("heading", name="Transaction", exact=True)).to_be_visible()
    expect(user.get_by_role("heading", name=PAYEE).first).to_be_visible()


def test_a_tag_added_on_a_transaction_shows_on_the_tags_page(user: Page, live: Live) -> None:
    tag = unique("Movie")
    user.goto(f"{live.url}/transactions?q=METREON")
    settle(user)
    user.get_by_role("button", name=f"Actions for {PAYEE}").first.click()
    user.get_by_role("menuitem", name="Counterparty").first.click()
    settle(user)
    user.get_by_role("textbox", name="New tag name").fill(tag)
    user.get_by_role("button", name="Add tag").click()
    settle(user)
    reload(user)
    expect(user.get_by_text(tag).first).to_be_visible()

    user.goto(f"{live.url}/settings/tags")
    expect(user.get_by_role("region", name="Tags")).to_contain_text(tag)
    expect(user.get_by_role("region", name="Tags")).to_contain_text("1 transaction")
