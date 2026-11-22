# Receipts

The Receipts page lists every receipt omsync holds, with search, filters and sort. A receipt's page
shows its items, tax, payments and the transactions it matched.

## Sub-features

- browser: "Search receipts", "Filter by vendor", "Filter by year", "Filter by status", "Filter by
  account", "Sort by".
- receipt-page: `/receipts/<vendor>/<receipt id>`: items, tax, payments, matched transactions; "Back".
- status: matched, waiting in triage, unmatched, paid elsewhere.

## How to get to it (user POV)

- The rail's Receipts (`/receipts`). A transaction's receipt link.

## Driving it with verify.py

Preconditions: receipts exist only if the data has a store with downloaded files. The demo data
may have none; check the empty state then.

- Search and filters narrow the list; reload keeps them.
- Open a receipt; expect its items and total; "Back" returns with filters kept.

## Gotchas

- Downloading receipts signs in to the store and is off here; it needs live validation.
