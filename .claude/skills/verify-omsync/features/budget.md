# Budget

The month's budget: income, fixed and flexible spending, non-monthly expenses, goal contributions,
and what is left.

## Sub-features

- month: `/plan/budget/<YYYY-MM>`; links "Previous month, <Month YYYY>" and "Next month, <Month
  YYYY>".
- mode: "Flex" and "Category" (pressed toggles); "Set flex number".
- income: "Save income".
- lines: per-category amounts that save when the field loses focus.
- non-monthly: region "Non-monthly expenses", saved each month and rolled over.
- summary: "Budget summary", totals, "Not budgeted".

## How to get to it (user POV)

- The rail's Plan → budget (`/plan/budget`), or Home.

## Driving it with verify.py

Preconditions: any mode. The demo month already has a budget.

- Change an amount, press Tab; the totals change; reload keeps it; Undo from the toast.
- "Save income" with a new number; reload.
- Toggle "Category" then "Flex"; the layout changes; reload keeps the mode.
- Previous month link, then Next month link; the heading's month follows.

## Gotchas

- A line saves on blur, not on a button; leave the field before checking.
- The quick-fill buttons ("Copy last month", "Quick fill with last budget", "Use average") show
  only on a month without a budget; go to a future month with "Next month" to reach them.
