# Tags

Labels you can put on any transaction, each with a color.

## Sub-features

- list: tags with their transaction counts, in a list panel.
- add: "Add tag" (header and empty state) → dialog with name and color → "Add tag".
- edit: a row's "Edit" icon → "Edit tag" → "Save".
- delete-unused: a row's "Delete" icon deletes at once; Undo.
- delete-used: a tag on transactions opens "Delete <name>" with "Move its transactions to"; "Delete".
- delete-all: "Delete all" (shown only when tags exist) → dialog "Delete all tags" → "Delete".

## How to get to it (user POV)

- Settings → Tags (`/settings/tags`).

## Driving it with verify.py

Preconditions: any mode. The demo starts with no tags.

- Add: `get_by_role("button", name="Add tag").first.click()` → fill "Name" → "Add tag"; the row
  shows; reload.
- Edit: the row's button named `Edit <tag>` → change name → "Save"; reload.
- Delete unused: `Delete <tag>` → the row goes → the toast's "Undo" → reload → it is back.
- Delete used: put the tag on a transaction first (transaction-detail.md), then `Delete <tag>`
  opens the dialog; choose "No tag. Just remove it." or another tag; "Delete".
- Delete all: "Delete all" → "Delete"; the empty state shows; Undo.

## Gotchas

- Rows have no ⋮ menu: Edit and Delete are icons named `Edit <tag>` and `Delete <tag>`.
- The list had a clipping bug once: open each row action and check the dialog is fully visible.
