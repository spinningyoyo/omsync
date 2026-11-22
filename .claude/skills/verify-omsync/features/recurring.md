# Recurring

Bills that repeat: the month's list with what is paid and what is left, the bills that need
attention, and suggested bills to review.

## Sub-features

- month: region "This month" with "Paid this month" (a progress bar) and one button per bill, named
  with its date, payee, state and amount (for example "Nov 12 Spotify Paid -$11.99").
- attention: region "Needs attention": a changed amount with "Not this bill" and "Use new amount".
- view: "View: Overview" switches views.
- add-bill: "Add bill" dialog.
- drawer: a bill's button opens its drawer: history, "Edit", "Skip", "Resume", "Delete".
- review: "Review N" for suggested bills.

## How to get to it (user POV)

- The rail's Recurring (`/recurring`). A transaction's Actions menu → "Mark as recurring".

## Driving it with verify.py

Preconditions: any mode. The demo has bills such as Rent, Netflix, Spotify, PG&E.

- Drawer: `page.get_by_role("button", name=re.compile("Spotify"))` opens it; expect its history.
- Edit: drawer → "Edit" → change → "Save"; reload.
- Delete: drawer → "Delete" (no dialog); the bill leaves the list; the toast's Undo brings it back.
- Needs attention: "Use new amount" on Netflix; the item leaves; reload; Undo.
- Add bill: "Add bill" → fill → "Add bill"; it appears in This month; reload.
- Review: "Review N" → accept or dismiss a suggestion; the count drops.

## Gotchas

- Dates and "paid" states are relative to today, so the list changes from day to day.
