"""Browser journeys on Data sync: stores, the vault, and the bank cards. Sync itself is off."""

import re

import pytest
from browser_support import SYNC_OFF, Live, expect_on_top, reload, settle, undo, unique
from playwright.sync_api import Page, expect
from web_support import PASSWORD

pytestmark = pytest.mark.browser


def _add_store(user: Page, live: Live) -> str:
    store = unique("Amazon")
    user.goto(f"{live.url}/settings/sync?tab=receipts")
    user.get_by_role("link", name="Add store").click()
    user.get_by_label("Store").select_option("amazon")
    user.get_by_label("Display name").fill(store)
    user.get_by_role("button", name="Continue").click()
    expect(user.get_by_role("heading", name="Vault login")).to_be_visible()
    user.get_by_role("button", name="Add store").click()
    user.wait_for_url(re.compile(r"/settings/sync(\?|$)"))
    settle(user)
    return store


def _card(user: Page, store: str):
    return user.locator("article", has=user.get_by_role("heading", name=store, exact=True))


def test_the_vault_unlocks_inside_the_edit_dialog_and_the_card_keeps_its_label(
    user: Page, live: Live
) -> None:
    store = _add_store(user, live)
    card = _card(user, store)
    expect(card.locator("dl")).to_contain_text("Vault login Not picked")

    user.get_by_role("button", name=f"Edit {store}").click()
    dialog = user.get_by_role("dialog", name=f"Edit {store}")
    password = dialog.get_by_label("Password", exact=True)
    expect_on_top(password)
    password.fill("wrong")
    dialog.get_by_role("button", name="Unlock").click()
    expect(dialog.get_by_role("alert")).to_contain_text("Incorrect master password")
    password.fill(PASSWORD)
    dialog.get_by_role("button", name="Unlock").click()
    expect(password).to_have_count(0)
    expect(user.get_by_role("button", name=re.compile("Vault unlocked"))).to_be_visible()

    dialog.get_by_role("searchbox").fill("amazon")
    dialog.get_by_role("radio", name="Amazon (Ana)").check()
    dialog.get_by_role("button", name="Save").click()
    reload(user)
    expect(card.locator("dl")).to_contain_text("Vault login Picked")

    user.get_by_role("button", name=re.compile("Vault unlocked")).click()
    user.get_by_role("button", name="Lock", exact=True).click()
    reload(user)
    expect(user.get_by_role("button", name=re.compile("Vault locked"))).to_be_visible()
    expect(card.locator("dl")).to_contain_text("Vault login Picked")

    live.vault.unlock(PASSWORD)
    reload(user)
    user.get_by_role("button", name=f"Edit {store}").click()
    dialog = user.get_by_role("dialog", name=f"Edit {store}")
    expect(dialog).to_contain_text("Vault login: Amazon (Ana)")


def test_sync_is_refused_with_its_reason(user: Page, live: Live) -> None:
    store = _add_store(user, live)
    live.vault.unlock(PASSWORD)
    user.get_by_role("button", name=f"Sync {store}").click()
    expect(user.locator("#sync-status")).to_contain_text(SYNC_OFF)


def test_deleting_a_store_asks_first_and_can_be_undone(user: Page, live: Live) -> None:
    store = _add_store(user, live)
    user.get_by_role("button", name=f"Delete {store}").click()
    dialog = user.get_by_role("dialog", name=f"Delete {store}?")
    expect_on_top(dialog.get_by_role("button", name="Delete"))
    dialog.get_by_role("button", name="Delete").click()
    expect(_card(user, store)).to_have_count(0)
    undo(user)
    reload(user)
    expect(_card(user, store)).to_have_count(1)


def test_disconnecting_the_vault_asks_first(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/settings/sync")
    live.vault.unlock(PASSWORD)
    reload(user)
    user.get_by_role("button", name=re.compile("Vault unlocked")).click()
    user.get_by_role("button", name="Disconnect").click()
    dialog = user.get_by_role("dialog", name="Disconnect the vault")
    expect_on_top(dialog.get_by_role("button", name="Disconnect"))
    dialog.get_by_role("button", name="Disconnect").click()
    settle(user)
    reload(user)
    expect(user.get_by_role("button", name=re.compile("No vault"))).to_be_visible()


def test_an_account_on_a_not_synced_card_is_renamed(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/settings/sync")
    expect(user.get_by_text("Not synced").first).to_be_visible()
    user.get_by_role("button", name="Rename Travel Savings").click()
    dialog = user.get_by_role("dialog", name="Rename account")
    dialog.get_by_label("Name").fill("Japan Fund")
    dialog.get_by_role("button", name="Save").click()
    reload(user)
    expect(user.get_by_role("button", name="Rename Japan Fund")).to_be_visible()
    panel = user.get_by_role("complementary").filter(has_text="Accounts").first
    expect(panel).to_contain_text("Japan Fund")
