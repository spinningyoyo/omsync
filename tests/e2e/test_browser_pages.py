"""Browser journeys across the other pages: every page opens with its main controls and no browser
errors, and the writes on Reconcile, Recurring and Goals stick and undo."""

import re

import pytest
from browser_support import Live, expect_on_top, name, reload, settle, undo, unique
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

# Each page and the controls the feature map names for it, found by role and visible name.
PAGES = [
    ("/", [("heading", "Home"), ("label", "Items that need attention")]),
    ("/reports", [("region", "Spending by category")]),
    ("/transactions", [("searchbox", "Search transactions"), ("button", "Saved filters")]),
    ("/recurring", [("button", "Add bill"), ("region", "This month")]),
    ("/reconcile", [("tab", name("Not matched")), ("tab", name("Linked"))]),
    ("/receipts", [("searchbox", "Search receipts")]),
    ("/plan/budget", [("button", "Save income"), ("link", name("Next month"))]),
    ("/plan/goals", [("button", "Add goal")]),
    ("/settings/sync", [("link", "Add bank"), ("button", "Sync all")]),
    ("/settings/sync?tab=receipts", [("link", "Add store")]),
    ("/settings/categories", [("button", "Add category")]),
    ("/settings/tags", [("button", "Add tag")]),
    ("/settings/contacts", [("button", "Add contact")]),
    ("/settings/rental", [("button", "Add property"), ("button", "Add category")]),
    ("/settings/guides", [("link", "Start here")]),
    ("/settings/about", [("heading", "About")]),
]


@pytest.mark.parametrize(("path", "controls"), PAGES, ids=[path for path, _ in PAGES])
def test_every_page_opens_with_its_controls_and_no_errors(
    user: Page, live: Live, path: str, controls: list
) -> None:
    errors: list[str] = []
    user.on("pageerror", lambda exc: errors.append(str(exc)))
    user.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    user.goto(f"{live.url}{path}")
    settle(user)
    for role, label in controls:
        found = user.get_by_label(label) if role == "label" else user.get_by_role(role, name=label)
        expect(found.first).to_be_attached()
    assert errors == []


def test_a_link_is_unlinked_and_undone_on_reconcile(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/reconcile")
    linked = user.get_by_role("tab", name=name("Linked"))
    count = int(re.findall(r"\d+", linked.inner_text())[-1])
    linked.click()
    user.get_by_role("button", name="Unlink").first.click()
    settle(user)
    reload(user)
    expect(user.get_by_role("tab", name=name("Linked"))).to_have_text(re.compile(f"{count - 1}"))
    user.get_by_role("tab", name=name("Linked")).click()
    user.get_by_role("button", name="Unlink").first.click()
    undo(user)
    reload(user)
    expect(user.get_by_role("tab", name=name("Linked"))).to_have_text(re.compile(f"{count - 1}"))


def test_a_bill_is_deleted_from_its_drawer_and_undone(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/recurring")
    user.get_by_role("button", name=re.compile(r"Spotify")).first.click()
    delete = user.get_by_role("button", name="Delete", exact=True)
    expect_on_top(delete)
    delete.click()
    settle(user)
    expect(user.get_by_role("button", name=re.compile(r"Spotify"))).to_have_count(0)
    undo(user)
    reload(user)
    expect(user.get_by_role("button", name=re.compile(r"Spotify")).first).to_be_visible()


def test_a_goal_is_added_and_deleted_after_asking(user: Page, live: Live) -> None:
    goal = unique("Bike")
    user.goto(f"{live.url}/plan/goals")
    user.get_by_role("button", name="Add goal").click()
    dialog = user.get_by_role("dialog", name="Add goal")
    dialog.get_by_label("Name").fill(goal)
    dialog.get_by_label("Target", exact=True).fill("500")
    dialog.get_by_role("button", name="Add goal").click()
    settle(user)
    user.goto(f"{live.url}/plan/goals")
    user.get_by_role("link", name=re.compile(goal)).first.click()
    settle(user)
    user.get_by_role("button", name="Delete", exact=True).click()
    dialog = user.get_by_role("dialog").filter(has=user.get_by_role("button", name="Delete"))
    expect_on_top(dialog.get_by_role("button", name="Delete"))
    dialog.get_by_role("button", name="Delete").click()
    settle(user)
    user.goto(f"{live.url}/plan/goals")
    expect(user.get_by_role("link", name=re.compile(goal))).to_have_count(0)
