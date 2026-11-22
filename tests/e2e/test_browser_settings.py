"""Browser journeys on the Settings pages: Tags, Rental properties, Contacts and Categories."""

import pytest
from browser_support import Live, expect_on_top, reload, settle, undo, unique
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser


def test_a_tag_is_added_edited_deleted_and_undone(user: Page, live: Live) -> None:
    tag = unique("Trip")
    user.goto(f"{live.url}/settings/tags")
    user.get_by_role("button", name="Add tag").first.click()
    dialog = user.get_by_role("dialog", name="Add tag")
    dialog.get_by_label("Name").fill(tag)
    dialog.get_by_role("button", name="Add tag").click()
    tags = user.get_by_role("region", name="Tags")
    expect(tags).to_contain_text(tag)
    reload(user)
    expect(tags).to_contain_text(tag)

    user.get_by_role("button", name=f"Edit {tag}").click()
    dialog = user.get_by_role("dialog", name="Edit tag")
    expect_on_top(dialog.get_by_label("Name"))
    dialog.get_by_label("Name").fill(f"{tag} 2")
    dialog.get_by_role("button", name="Save").click()
    reload(user)
    expect(tags).to_contain_text(f"{tag} 2")

    user.get_by_role("button", name=f"Delete {tag} 2").click()
    expect(tags).not_to_contain_text(tag)
    undo(user)
    reload(user)
    expect(tags).to_contain_text(f"{tag} 2")


def test_delete_all_tags_asks_first_and_can_be_undone(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/settings/tags")
    expect(user.get_by_role("button", name="Delete all")).to_have_count(0)
    user.get_by_role("button", name="Add tag").first.click()
    user.get_by_role("dialog", name="Add tag").get_by_label("Name").fill("Gift")
    user.get_by_role("dialog", name="Add tag").get_by_role("button", name="Add tag").click()
    reload(user)

    user.get_by_role("button", name="Delete all").click()
    dialog = user.get_by_role("dialog", name="Delete all tags")
    expect_on_top(dialog.get_by_role("button", name="Delete"))
    dialog.get_by_role("button", name="Delete").click()
    expect(user.get_by_text("No tags yet.")).to_be_visible()
    undo(user)
    reload(user)
    expect(user.get_by_role("region", name="Tags")).to_contain_text("Gift")


def test_a_rental_property_is_added_relabelled_removed_and_undone(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/settings/rental")
    user.get_by_role("button", name="Add property").click()
    dialog = user.get_by_role("dialog", name="Add property")
    dialog.get_by_label("Account").select_option(label="Everyday Checking ••4821")
    dialog.get_by_label("Label").fill("Elm Street")
    dialog.get_by_role("button", name="Add property").click()
    expect(user.get_by_role("status")).to_contain_text("Added Elm Street as a rental property")
    reload(user)
    expect(user.get_by_text("Elm Street", exact=True)).to_be_visible()

    user.get_by_role("button", name="Edit Elm Street").click()
    dialog = user.get_by_role("dialog", name="Edit Elm Street")
    dialog.get_by_label("Label").fill("Elm St")
    dialog.get_by_role("button", name="Save").click()
    reload(user)
    expect(user.get_by_text("Elm St", exact=True)).to_be_visible()

    user.get_by_role("button", name="Remove Elm St").click()
    expect(user.get_by_text("No rental properties yet.")).to_be_visible()
    undo(user)
    reload(user)
    expect(user.get_by_text("Elm St", exact=True)).to_be_visible()


def test_the_last_rental_category_cannot_be_removed(user: Page, live: Live) -> None:
    user.goto(f"{live.url}/settings/rental")
    user.get_by_role("button", name="Add category").click()
    dialog = user.get_by_role("dialog", name="Add category")
    dialog.get_by_label("Category").select_option(label="Home")
    dialog.get_by_role("button", name="Add category").click()
    reload(user)
    user.get_by_role("button", name="Remove Home").click()
    expect(user.get_by_role("alert")).to_have_text("Keep at least one rental category.")
    reload(user)
    expect(user.get_by_role("button", name="Remove Home")).to_be_visible()


def test_a_contact_is_added_named_edited_and_deleted(user: Page, live: Live) -> None:
    contact = unique("Sam")
    user.goto(f"{live.url}/settings/contacts")
    user.get_by_role("button", name="Add contact").click()
    dialog = user.get_by_role("dialog", name="Add contact")
    dialog.get_by_label("Name", exact=True).fill(contact)
    dialog.get_by_role("button", name="Add contact").click()
    settle(user)
    user.goto(f"{live.url}/settings/contacts")
    user.get_by_role("link", name=contact).click()
    expect(user.get_by_role("heading", name=contact, level=1)).to_be_visible()

    user.get_by_role("button", name="Add name").click()
    dialog = user.get_by_role("dialog", name="Add name")
    dialog.get_by_label("Given name").fill("Samwise")
    dialog.get_by_role("button", name="Add name").click()
    reload(user)
    names = user.get_by_role("region", name="Names")
    expect(names).to_contain_text("Samwise")
    user.get_by_role("button", name="Delete Samwise").click()
    expect(names).not_to_contain_text("Samwise")
    undo(user)
    reload(user)
    expect(user.get_by_role("region", name="Names")).to_contain_text("Samwise")

    user.get_by_role("button", name="Edit", exact=True).click()
    dialog = user.get_by_role("dialog", name=f"Edit {contact}")
    dialog.get_by_label("Name", exact=True).fill(f"{contact} B")
    dialog.get_by_role("button", name="Save").click()
    reload(user)
    expect(user.get_by_role("heading", name=f"{contact} B", level=1)).to_be_visible()

    user.get_by_role("button", name="Delete", exact=True).click()
    dialog = user.get_by_role("dialog", name=f"Delete {contact} B")
    expect_on_top(dialog.get_by_role("button", name="Delete"))
    dialog.get_by_role("button", name="Delete").click()
    settle(user)
    user.goto(f"{live.url}/settings/contacts")
    expect(user.get_by_role("link", name=f"{contact} B")).to_have_count(0)


def test_an_unused_category_is_deleted_without_asking_and_undone(user: Page, live: Live) -> None:
    category = unique("Hobbies")
    user.goto(f"{live.url}/settings/categories")
    user.get_by_role("button", name="Add category").first.click()
    dialog = user.get_by_role("dialog", name="Add category")
    dialog.get_by_label("Name").fill(category)
    dialog.get_by_role("button", name="Add category").click()
    settle(user)
    delete = user.get_by_role("button", name=f"Delete {category}", exact=True)
    reload(user)
    expect(delete).to_be_visible()

    delete.click()
    expect(delete).to_have_count(0)
    undo(user)
    reload(user)
    expect(delete).to_be_visible()
