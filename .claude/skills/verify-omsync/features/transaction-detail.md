# Transaction detail

One transaction, edited in two places. Its row on the register sets the category, opens the split
editor and has an Actions menu. Its own page shows the facts and edits the tags and the
counterparty.

## Sub-features

- row-category: the row's inline "Category" select.
- row-split: the row's "Split <payee>" opens the split editor in place: "Add line", "Line amount",
  "Line category", "Line memo", "Save split", and "Remove" to go back to one line.
- row-actions: "Actions for <payee>": "Edit memo…", "Mark as recurring", "Counterparty" (opens the
  page).
- page: `/transactions/<id>/detail`: heading "Transaction", the payee as a heading, Date, Account,
  Amount, Category; "Back".
- tags: section "Tags": "New tag" field, color radios, "Add tag"; a tag pill's remove; "Manage tags"
  links to Settings → Tags.
- counterparty: section "Counterparty": "Set the counterparty of the transaction to" → "Set";
  "Clear" once set.

## How to get to it (user POV)

- On `/transactions`, a row's "Actions for <payee>" → "Counterparty" opens the page.
- The row itself: its Category select and its "Split <payee>" button.

## Driving it with verify.py

Preconditions: any mode. Pick a plain expense row, such as "AMC METREON 16".

- Category: the row's `get_by_label("Category")` → another option; reload; it stays; Undo; reload.
- Split: "Split <payee>" → "Add line" → amounts that add up → categories → "Save split"; reload;
  two lines; "Remove" → one line again. Amounts that do not add up keep "Save split" disabled.
- Memo: Actions → "Edit memo…" → text → "Save"; reload.
- Page: Actions → "Counterparty"; expect the heading "Transaction" and the payee heading.
- Tags: on the page, fill "New tag" → "Add tag"; the pill shows; reload; Settings → Tags shows it
  with count 1.
- Counterparty: choose a contact in "Set the counterparty of the transaction to" → "Set"; reload;
  "Clear"; reload.

## Gotchas

- Rows are not links; open the page through the Actions menu.
- The page's Category is read-only text. Set the category on the register row.
- A receipt-applied transaction shows receipt lines; pick a plain transaction for split checks.
