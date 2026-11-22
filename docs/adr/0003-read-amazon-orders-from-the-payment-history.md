---
status: accepted
---

# Read Amazon orders from the payment history instead of the data export

## Context

Through v0.2.1, omsync reads Amazon orders from a CSV data export. You request your data on
Amazon's "Request my data" page. Amazon sends the files some days later. You put
`Order History.csv` and `Refund Details.csv` in the receipts folder by hand.

Amazon's payment history is available through web requests after a sign-in. It gives each card
charge with its amount, its date, and the last four digits of its card. It also gives each refund
with the order that it belongs to.

The payment history is better than the data export:

- A sync fetches orders right away. You do not request an export and wait for Amazon to send it.
- Each charge has its own amount. The export does not say which charge paid which shipment.
- An order paid in part with a gift card has a charge amount for the card portion. The export does
  not give the gift-card amount.
- Refunds name the order that they belong to.

## Decision

Read Amazon orders from the payment history through a sync. Stop reading the CSV data export.

## Consequences

- omsync cannot read a data export any more. An order that only the export has is not a receipt.
- A change to the Amazon website can stop the sync. A change to the Amazon connector needs a live
  test at Amazon.
