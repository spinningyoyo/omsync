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


def test_receipt_link_opens_in_new_tab_directly(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/transactions")
    settle(user)
    user.get_by_label("Rows per page").select_option("200")
    settle(user)
    for vendor in ("costco", "amazon"):
        receipt = user.locator(f"table.register a.link-btn[href^='/receipts/{vendor}/']").first
        expect(receipt).to_be_visible()
        expect(receipt).to_have_attribute("target", "_blank")
        with user.expect_popup() as popup_info:
            receipt.click()
        popup = popup_info.value
        popup.wait_for_load_state("networkidle")
        expect(popup).to_have_url(re.compile(rf"/receipts/{vendor}/[^/]+"))
        popup.close()


def test_last_page_date_sort_and_between_filter(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/transactions")
    settle(user)
    user.get_by_label("Rows per page").select_option("25")
    settle(user)

    # Last page
    pager = user.get_by_role("navigation", name="Pages")
    pager.get_by_role("button", name="Last").click()
    settle(user)
    page_text = pager.locator("span.tabular-nums").inner_text().strip()
    current_p, total_p = [int(part.strip()) for part in page_text.split("/")]
    assert current_p == total_p and total_p > 1
    expect(pager.get_by_role("button", name="Last")).to_be_disabled()
    expect(pager.get_by_role("button", name="Next")).to_be_disabled()

    # Date sort asc / desc
    user.goto(f"{live.url}/transactions")
    settle(user)
    date_th = user.locator("table.register thead th[aria-sort]")
    expect(date_th).to_have_attribute("aria-sort", "descending")
    desc_dates = user.locator("table.register tbody td.date").all_inner_texts()
    date_th.get_by_role("button", name="Date").click()
    settle(user)
    expect(user).to_have_url(re.compile(r"sort=date_asc"))
    expect(user.locator("table.register thead th[aria-sort]")).to_have_attribute(
        "aria-sort", "ascending"
    )
    asc_dates = user.locator("table.register tbody td.date").all_inner_texts()
    assert asc_dates == sorted(asc_dates)
    assert asc_dates[0] < desc_dates[0]

    # Date is between filter
    user.get_by_role("button", name="+ Filter").click()
    user.get_by_role("menuitem", name="Date").click()
    settle(user)
    form = user.locator("#filter-form")
    form.locator("select[name='op']").select_option("between")
    from_input = form.locator("input[name='value']")
    to_input = form.locator("input[name='value_to']")
    expect(from_input).to_be_visible()
    expect(to_input).to_be_visible()
    start_date, end_date = asc_dates[0], asc_dates[min(5, len(asc_dates) - 1)]
    from_input.fill(start_date)
    to_input.fill(end_date)
    form.get_by_role("button", name="Add filter").click()
    settle(user)
    expect(user.locator("#filter-chips button", has_text="Date between").first).to_be_visible()
    for shown in user.locator("table.register tbody td.date").all_inner_texts():
        assert start_date <= shown <= end_date
