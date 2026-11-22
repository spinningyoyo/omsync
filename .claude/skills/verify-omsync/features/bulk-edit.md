# Bulk edit

Selecting rows in the register shows a bar of actions that change all of them as one undoable write.

## Sub-features

- select: row check boxes, "Select all rows on screen", "Select all …", "Clear selection".
- set-category: "Set category…".
- clear-category: "Clear category".
- set-memo: "Set memo…".
- tags: "Add tag…" and "Remove tag…".

## How to get to it (user POV)

- `/transactions`, tick one or more rows.

## Driving it with verify.py

Preconditions: any mode. Note two rows' payees first, to find them again after reload.

- Select two rows with their check boxes; the bar shows the count.
- "Set category…" → choose a category → apply; both rows show it; reload; Undo; reload; the old
  categories are back.
- "Add tag…" → create or pick a tag → apply; both rows show the tag pill; "Remove tag…" takes it
  off.
- "Clear selection" empties the bar.

## Gotchas

- A bulk write is one batch, so one Undo reverses all rows.
- Tags must exist or be created in the picker ("Add tag" in the picker).
