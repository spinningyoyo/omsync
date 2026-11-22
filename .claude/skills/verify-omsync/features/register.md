# Transactions register

The Transactions page lists every transaction with search, filters, saved filters and pages. A row
opens its detail; check boxes select rows for bulk edits.

## Sub-features

- list: rows with date, account, payee, tags, category, amount. Each row has a check box "Select
  <payee>", an inline "Category" select, "Tags for <payee>", "Actions for <payee>" (a menu) and
  "Split <payee>".
- search: "Search transactions".
- filters: "Add filter" / a filter chip's remove; the filter form.
- saved-filters: the "Saved filters" menu: apply, "Save as…", "Update “name”", "Delete".
- pages: "Prev", "Next", rows per page.
- account-scope: `/transactions?account=<id>` from an account's View transactions.

## How to get to it (user POV)

- The rail's Transactions (`/transactions`). An account page's "View transactions".

## Driving it with verify.py

Preconditions: any mode.

- Search: fill `get_by_role("searchbox", name="Search transactions")` with a payee word; expect only
  matching rows; clear it; the full list returns.
- Filter: "Add filter", pick a field and value, "Add filter"; a chip appears and rows narrow; remove
  the chip.
- Save filter: with a filter on, "Saved filters" → "Save as…" → name → "Save"; the menu shows the
  name; reload and apply it from the menu.
- Delete saved filter: apply it, menu → "Delete"; it leaves the menu; the toast's Undo restores it.
- Inline category: the row's "Category" select → another category; reload; Undo.
- Open a transaction: the row's "Actions for <payee>" → "Counterparty" opens its detail page.
- Pages: "Next" then "Prev"; the row range text changes.

## Gotchas

- Filters live in the URL; reload keeps them, which is the persistence check.
- The saved-filter menu's Delete posts at once (no dialog); its toast is the safeguard.
