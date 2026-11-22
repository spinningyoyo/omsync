# Categories

Categories and what sets them: keywords (payee and receipt lists), rules, the triage of payees with
no category, icons, hide, and delete with a move.

## Sub-features

- categories: the list, "Add category", "Set type", "Hide", "Show", icons ("Search icons", "Use
  default icon").
- delete: a category's "Delete" icon: unused → deleted at once with Undo; in use → the dialog that
  moves its uses, with "Delete".
- keywords: "Keyword lists" (payee, receipt), "Add keyword", "Edit keyword", "Delete", "Import
  keywords" → "Preview import" → "Confirm and Import", "Test a payee".
- rules: "Add rule" form, "Preview", "Add rule", "Move up"/"Move down", "Edit rule", "Delete".
- triage: "Uncategorized payees" with "Add keyword" and "Keep category".

## How to get to it (user POV)

- Settings → Categories (`/settings/categories`), with tabs for categories, keywords, rules, triage.

## Driving it with verify.py

Preconditions: any mode.

- Add category: "Add category" → name and type → "Add category"; reload; listed.
- Delete unused: its "Delete" icon; it goes with no dialog; Undo brings it back.
- Delete in use: the icon opens the move dialog; pick a target; "Delete"; its transactions move;
  Undo.
- Keyword: "Add keyword" form → pattern and category → "Add keyword"; the register's matching
  payees take the category; "Delete" (no dialog); Undo.
- Rule: fill the "Add rule" form → "Preview" lists "Transactions this would change" → "Add rule";
  move it up and down; "Delete"; Undo.

## Gotchas

- Keyword and rule writes re-categorize transactions; check a register row, not only the table.
