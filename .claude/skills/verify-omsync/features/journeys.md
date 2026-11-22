# Journeys

Paths that cross pages, where one page's write must show on another.

## Sub-features

- tag-across: a tag added on a transaction shows on Tags with count 1, and Delete all takes it off
  the transaction.
- category-across: a keyword added on Categories categorizes register rows; deleting the category
  with a move moves them.
- vault-across: unlocking in a dialog turns the header to "Vault unlocked", and cards keep their
  "Picked" label through lock and unlock.
- rental-across: adding a rental property enables the rental export on Reports.
- undo-across: Undo from a toast on one page restores what another page shows.

## How to get to it (user POV)

- As each feature file says; these chain them.

## Driving it with verify.py

Preconditions: ledger mode, a fresh launch.

- tag-across: transaction detail → "Edit tags" → new tag → back to `/settings/tags`: count 1 →
  "Delete all" → "Delete" → the transaction has no tag → Undo → it is back.
- vault-across: `test_browser_sync.py` unlocks in the Edit dialog, then locks from
  the header and check the card still reads "Vault login Picked".
- rental-across: rental.md's Add property → Reports → "Export…" offers the rental workbook.

## Gotchas

- Each journey writes; run them on a fresh launch so one does not change another's start.
