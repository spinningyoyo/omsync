# Data sync: banks

The Banks tab of Data sync lists each bank provider as a card: its login, its last sync and the
accounts it fetches. Institutions with accounts that no provider fetches show a Not synced card.

## Sub-features

- bank-list: provider cards and Not synced cards, in a grid.
- add-bank: the Add bank wizard (bank and name, sign-in, accounts, file checks).
- edit-bank: the Edit dialog: name, vault login, accounts it fetches.
- delete-bank: Delete asks in a dialog, because a provider's sync history goes with it.
- sync-bank: the card's Sync icon and the header's Sync all.
- rename-account: the pencil on an account row renames the account.

## How to get to it (user POV)

- User menu → Data sync, or Settings → Data sync (`/settings/sync`). The Banks tab is first.

## Driving it with verify.py

Preconditions: ledger mode. The demo data has accounts but no bank providers, so Bank of America,
Chase and Citi each show a Not synced card.

- Not synced: `page.goto(base + "/settings/sync")`; expect a card per institution with "Not
  synced" and its accounts.
- Add bank: `page.get_by_role("link", name="Add bank").click()`; the wizard is at
  `/settings/sync/new/bank`. Step through with "Continue", "Save and continue", "Save accounts";
  then the card shows the accounts chosen, and the Not synced card loses them.
- Rename account: on an account row, the pencil "Rename" opens "Rename account"; Save; reload and
  check the new name on the card and in the accounts panel.
- Edit: the card's "Edit" icon opens `Edit <name>`; change Name; Save; reload.
- Delete: the card's "Delete" icon opens `Delete <name>?`; Delete; the card goes; the toast's Undo
  brings it back.
- Sync: the "Sync" icon or "Sync all" answers "Sync is off in verification runs". Report sync as
  not reached.

## Gotchas

- A real sync signs in to the bank. It is off here and needs live validation at the bank.
- The card holds its Edit and Delete dialogs, so text searches inside a card can match twice.
  Scope to the card's `dl` or use `exact=True`.
