# Rental properties

Which accounts are rental properties, with their name in the rental report, and which categories
count as rental expenses.

## Sub-features

- properties: list panel; "Add property" dialog (Account, Label); a row's "Edit" icon (Label →
  "Save") and "Remove" icon.
- categories: list panel; "Add category" dialog (Category); a row's "Remove" icon.
- last-category: removing the last rental category is refused: "Keep at least one rental category."

## How to get to it (user POV)

- Settings → Rental properties (`/settings/rental`). Reports' export says to set it up.

## Driving it with verify.py

Preconditions: any mode.

- Add property: "Add property" → Account → Label → "Add property"; the row shows the label and the
  account; the toast reads "Added <label> as a rental property"; reload.
- Edit: `Edit <label>` → change Label → "Save"; reload.
- Remove: `Remove <label>` → the row goes → "Undo" → reload → back.
- Categories: "Add category" → choose → "Add category"; then remove all but one; the last is
  refused with the message above.

## Gotchas

- Remove takes the account off the list; the account and its transactions stay.
- With no accounts left to add, "Add property" is hidden.
