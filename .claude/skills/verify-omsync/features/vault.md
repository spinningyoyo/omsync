# Vault

Bitwarden fills bank and store logins. The header button on Data sync shows the vault's state and
opens its panel: Connect, Unlock, Lock, Disconnect. A login search that finds the vault locked shows
an unlock form in its own place, so a dialog never sends you behind itself.

## Sub-features

- status: the header button reads "Vault locked" or "Vault unlocked", with a grey or green dot.
- panel-unlock: unlock from the header panel.
- inline-unlock: the unlock form in place of login results, in Edit and both Add wizards.
- wrong-password: a wrong master password shows "Incorrect master password" where it was typed.
- card-label: cards say "Vault login Picked" or "Not picked", the same whether locked or not.
- login-name: an open Edit dialog shows the login's name once the vault is unlocked.
- lock: Lock in the panel.
- disconnect: Disconnect asks in a dialog, then reloads with no vault.
- connect: with no vault, the panel's Connect form (email and master password).

## How to get to it (user POV)

- `/settings/sync`: the header's Bitwarden button.
- Any Edit on a store or bank card, and the Add bank and Add store wizards' login steps.

## Driving it with verify.py

Preconditions: ledger mode. The vault starts locked. Master password: `MASTER_PASSWORD`
(`verify-master`). Logins: `Chase (verify)`, `Amazon (verify)`, `Costco (verify)`, `Bank of America
(verify)`. Journey: `test_browser_sync.py`.

- Inline unlock: open a card's Edit; in the dialog expect "Vault locked. Enter your master
  password"; fill `get_by_label("Password", exact=True)` with "wrong", click "Unlock", expect the
  alert "Incorrect master password"; then the right password; expect a status "Vault unlocked".
- Header agrees: after that, the header button's name contains "Vault unlocked".
- Pick: fill the dialog's searchbox with "amazon", check the radio "Amazon (verify)", "Save";
  reload; the card's `dl` reads "Vault login Picked"; reopen Edit and expect "Amazon (verify)".
- Lock: header button → "Lock"; the header reads "Vault locked"; cards still read "Picked".
- Disconnect: header button → "Disconnect" → dialog "Disconnect the vault" → "Disconnect"; the
  page reloads and the header reads "No vault"; cards show the sign-in line instead.
- Connect: header button → Email `verify@example.test`, Password `MASTER_PASSWORD` → "Connect".

## Gotchas

- The stand-in vault answers searches but never gives a password, so nothing can sign in with it.
- In demo mode the vault refuses every unlock; use ledger mode.
- The unlock form's password field is labelled "Password"; use `exact=True` or the Show password
  button's name matches too.
