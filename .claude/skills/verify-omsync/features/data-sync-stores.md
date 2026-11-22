# Data sync: stores

The Receipts tab of Data sync lists receipt stores (Amazon, Costco) as cards: the vault login, the
last sync, the receipt count and the latest receipt date.

## Sub-features

- store-list: one card per store provider.
- add-store: the Add store wizard: Store and name, then Vault login (or Sign-in without a vault).
- edit-store: Edit: name and vault login.
- delete-store: Delete in a dialog.
- sync-store: the Sync icon (off in verification runs).

## How to get to it (user POV)

- `/settings/sync`, then the Receipts tab (`/settings/sync?tab=receipts`).

## Driving it with verify.py

Preconditions: ledger mode. No stores exist until a drive adds one. Journey:
`test_browser_sync.py`.

- Add store: "Add store" link → `get_by_label("Store").select_option("amazon")`, fill "Display
  name", "Continue" → heading "Vault login" → "Add store". The page returns to Data sync and the
  card shows "Vault login Not picked" in its `dl`.
- Edit with a login: see vault.md, unlock in place.
- Delete: the "Delete" icon → dialog → "Delete"; the card goes; Undo brings it back.

## Gotchas

- After "Add store" the page redirects; wait for `/settings/sync` with a regex before the next
  `goto`, or the navigation is aborted.
- Use a unique display name per drive; a second store with the same name makes card lookups
  ambiguous.
