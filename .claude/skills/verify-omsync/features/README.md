# omsync feature map

Every page and action the owner uses, grouped as the app's navigation groups them. Each file says
how to reach a feature as a person would, and how to drive and check it with the `verify.py` drive
globals (see [`../SKILL.md`](../SKILL.md)).

## Baseline preconditions

- A run from `verify.py launch` (ledger mode unless the file says demo), and `verify.py doctor RUN`
  passing.
- Signed in: `drive` signs in before your script runs. The page starts at `/`.
- The data is the demo's made-up year: accounts Everyday Checking (••4821), Emergency Fund
  (••9034), Travel Savings (••5560), Double Cash (••3310) and Sapphire Card (••7702), at Bank of
  America, Chase and Citi, with contacts such as Priya Nair. There are no providers and no tags
  until a drive adds them.
- Find controls by role and visible name. The names in these files are the ones on screen today.

## Proof and skip reporting

- Exercise every way in that a file lists, and the success, cancel, refusal, empty and
  after-reload paths of each action.
- Where a toast offers Undo, undo it and reload.
- Name each path you could not reach and why (sync is off, the demo vault refuses, needs the owner
  at the Mac). Never report it as verified through another path.

## Full sweep

Walk the groups below top to bottom, then [journeys.md](journeys.md).

## Sign-in and shell

- [sign-in.md](sign-in.md): Sign in, Explore the demo, sign-out.
- [shell.md](shell.md): the rail, the accounts panel, theme and privacy, Guides, undo toasts.

## Money in

- [data-sync-banks.md](data-sync-banks.md): add, edit, sync and delete banks; Not synced cards.
- [data-sync-stores.md](data-sync-stores.md): add, edit, sync and delete receipt stores.
- [vault.md](vault.md): connect, unlock (header and in place), lock and disconnect Bitwarden.
- [account.md](account.md): an account's page, numbers and nicknames.

## Transactions

- [register.md](register.md): the transactions list, search, filters, saved filters, pages.
- [bulk-edit.md](bulk-edit.md): select rows and set a category, memo or tags.
- [transaction-detail.md](transaction-detail.md): one transaction: category, split, tags, memo,
  counterparty.
- [import-review.md](import-review.md): transactions waiting for review after an import.

## Matching

- [reconcile.md](reconcile.md): proposals, links, not matched and explained transactions.
- [receipts.md](receipts.md): the receipts browser and one receipt.
- [recurring.md](recurring.md): bills, their review and their drawer.

## Reports and plan

- [home.md](home.md): the dashboard and its attention list.
- [reports.md](reports.md): reports, print and the Excel exports.
- [budget.md](budget.md): the month's budget.
- [goals.md](goals.md): goals, one goal, archive and delete.

## Settings

- [categories.md](categories.md): categories, keywords, rules, triage, delete and move.
- [tags.md](tags.md): add, edit, delete and delete all.
- [contacts.md](contacts.md): the list and one contact's page.
- [rental.md](rental.md): rental properties and rental categories.
- [guides-about.md](guides-about.md): Guides and About.

## Journeys

- [journeys.md](journeys.md): paths that cross pages.

## Journeys that check these features

The fixed checks are in `tests/e2e/test_browser_*.py` (`uv run pytest -m browser`). Each feature
file's "Driving it" steps match a journey there, where one exists:

| Test file | Features it checks |
|---|---|
| `test_browser_settings.py` | tags, rental, contacts, categories (add, delete, undo) |
| `test_browser_sync.py` | vault (unlock in Edit, lock, disconnect), data-sync-stores, data-sync-banks (rename) |
| `test_browser_transactions.py` | register (search, category, actions menu), transaction-detail (tags) |
| `test_browser_pages.py` | every page's main controls with no browser errors; reconcile (unlink); recurring (delete, undo); goals (add, delete) |
| `test_sign_in_shell.py` | sign-in |

A feature with no journey yet (bulk edit, splits, saved filters, budget lines, import review,
receipts, reports export, account numbers) is driven with this skill until its journey is written.

## Entry contract

Every feature file has these four H2s, in this order:

1. `Sub-features`: short IDs, one line each.
2. `How to get to it (user POV)`: every way in.
3. `Driving it with verify.py`: preconditions, then each action with its check.
4. `Gotchas`: traps that waste or invalidate a run.
