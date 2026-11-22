# Reconcile

Reconcile pairs money that moved between your accounts and to contacts: transfers, refunds, P2P
payments and receipts. Its tabs hold the tasks for a period: transactions with no counterpart, and
the links already made.

## Sub-features

- period: the "Period: Last 90 days" button changes the period.
- tasks: tablist "Reconcile tasks": "Not matched N" and "Linked N" (the counts are in the names).
- not-matched: region "Not matched transactions": tick rows, then "Counterparty of the selected
  rows" (a contact or "A new contact…" with "New contact's name" and "Kind") → "Set counterparty";
  per row "Link manually…".
- linked: the Linked tab: each link with "Unlink".
- proposals: when omsync finds pairs it cannot apply alone, "Approve", "Reject", "Approve selected",
  "Reject selected".

## How to get to it (user POV)

- The rail's Reconcile (`/reconcile`). Home's attention items link here.

## Driving it with verify.py

Preconditions: any mode. The demo shows "Not matched 1" and "Linked 11" on a fresh launch.

- Tabs: `page.get_by_role("tab", name=re.compile("^Linked"))` → the links list; back to
  `re.compile("^Not matched")`.
- Set counterparty: tick the row's check box, choose "A new contact…" in "Counterparty of the
  selected rows", fill "New contact's name", "Set counterparty"; the row leaves Not matched; the
  contact exists on Contacts; Undo.
- Unlink: in Linked, "Unlink" on one; the Linked count drops by one; reload; Undo; the count is back.
- Link manually: "Link manually…" → pick a counterpart → "Link"; reload.

## Gotchas

- Tab names carry counts, so match them with a regex.
- Proposals show only when there are some; a fresh demo may have none. Report them as not reached
  then.
