# Shell

What every signed-in page shares: the rail, the accounts panel, the top bar's Guides and privacy
controls, the user menu with theme and appearance, and the undo toasts that writes show.

## Sub-features

- rail: Home, Reports, Transactions, Recurring, Reconcile, Receipts, Plan; the active item is marked.
- accounts-panel: accounts by type or by institution, each with its balance and sparkline; resize
  and close.
- privacy: the eye button blurs every amount ("Privacy mode: blur amounts").
- theme: light and dark from the user menu's Appearance.
- guides-drawer: Guides in the top bar opens short walkthroughs.
- toasts: a write shows a toast at the bottom; most have Undo.

## How to get to it (user POV)

- Any page after sign-in. The rail is on the left, the accounts panel beside it.
- The ⋮ at the foot of the rail opens the user menu (Settings, Guides, Data sync, Appearance).

## Driving it with verify.py

Preconditions: none beyond sign-in.

- Rail: `page.get_by_role("navigation", name="Main").get_by_role("link", name="Transactions").click()`;
  expect the URL `/transactions` and the link marked current.
- Accounts panel: `page.get_by_role("button", name="By institution").click()` (a pressed toggle
  beside "By type"); expect the group names Bank of America, Chase and Citi. An account row opens
  `/accounts/<id>`.
- Privacy: click the button named "Privacy mode"; amounts get the blur class; click again to undo.
  `drive` turns privacy off before each run.
- Theme: run `drive ... --theme dark` and compare screenshots; check text stays readable.
- Toast and Undo: covered in each feature that writes (tags.md is the quickest).

## Gotchas

- Privacy mode and theme live in localStorage, per browser; `drive` resets both.
- The accounts panel can be closed ("Close accounts"); reload brings back the saved state, so a
  later drive may start with it closed.
