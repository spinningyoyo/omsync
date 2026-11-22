# Home

The dashboard: net worth and cash flow, outflows by category, items that need attention, goals.

## Sub-features

- summary: totals and charts ("Outflows by category").
- attention: "Items that need attention", each with its action ("Open recurring", "Go to Settings,
  Bank sync").
- sections: "Home sections"; "Expand all".
- goals: "All goals".

## How to get to it (user POV)

- The rail's Home (`/`), where sign-in lands.

## Driving it with verify.py

Preconditions: any mode.

- Expect the heading "Home" and the region "Items that need attention".
- Each attention item's link opens the page it names; go back.
- `shot("home")` in light and with `--theme dark`.

## Gotchas

- Amounts depend on today's date, since the demo is built for the last year.
