# Reports

Spending by category and over time, budget performance, and two Excel workbooks: the year's
transactions and the rental report.

## Sub-features

- charts: "Spending by category", budget performance, the tags filter.
- period: the period controls and "Show".
- print: menu → "Print" (`/reports/print`).
- export: menu → "Export…": transactions workbook; rental workbook only when rental properties are
  set ("Set up rental properties in Settings" otherwise).

## How to get to it (user POV)

- The rail's Reports (`/reports`).

## Driving it with verify.py

Preconditions: any mode. For the rental workbook, first add a property (rental.md).

- Change the period → "Show"; the charts change; reload keeps it.
- Export: menu → "Export…" → choose the transactions workbook → download with
  `with page.expect_download() as d:`; check the file name `transactions_YYYY.xlsx` and that it
  opens with openpyxl.
- Rental: with no properties, the rental item says to set them up; after adding one, it downloads
  `rental_YYYY.xlsx`.

## Gotchas

- Downloads land in Playwright's temp folder; save them into `evidence/` with `d.value.save_as`.
