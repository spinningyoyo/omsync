# Import review

After an import, transactions omsync is unsure of (a possible duplicate, an unknown account number)
wait on the Review page with choices.

## Sub-features

- review-list: waiting items with their reason.
- resolve: "Same as …" (it is a duplicate) or "Add as new transaction".

## How to get to it (user POV)

- `/transactions/review`, linked from the register and Home when items wait.

## Driving it with verify.py

Preconditions: items to review exist only after an import that produced them. The demo data
usually has none.

- Open `/transactions/review`; with no items, expect the empty state.
- With items: "Add as new transaction" → the item leaves; the register shows it; Undo.

## Gotchas

- Producing a review item needs a second statement file with an overlapping transaction; this
  setup does not create one. Report resolve as not reached unless the data has items.
