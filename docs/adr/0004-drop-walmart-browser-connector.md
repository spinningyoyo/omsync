---
status: accepted
---

# Keep the Walmart receipt reader and remove the browser connector

## Context

omsync added a Walmart receipt connector alongside a backend reader (`WalmartVendor`) and payee
matching rules. The reader parses saved Walmart order JSON files and matches them to card charges.
The connector signs in to `www.walmart.com` and calls Walmart's `PurchaseHistoryV3` and `getOrder`
GraphQL endpoints inside the browser tab.

Walmart protects its site with PerimeterX. Live tests showed a hard boundary:

- A clean Chrome launch (without `--remote-debugging-port`) lets you sign in and view orders
  without a block.
- As soon as omsync opens Chrome with `--remote-debugging-port` or attaches Selenium
  (`chrome-attach`) to run the GraphQL requests, PerimeterX redirects the session to
  `walmart.com/blocked` with an unsolvable Press & Hold challenge and taints the browser profile.
- Even raw Chrome DevTools Protocol without ChromeDriver or `Runtime.enable` still triggers
  `walmart.com/blocked` after clean sign-in.

Without remote debugging, Chrome offers no programmatic channel for omsync to drive requests
inside a clean tab (branded Google Chrome also removed `--load-extension`), and omsync does not
automate or evade anti-bot challenges.

## Decision

Keep the Walmart backend receipt reader and ledger matching (`WalmartVendor` and
`is_walmart_payee`), and remove the browser connector (`WalmartConnector`) and its sync and fetch
entry points.

## Consequences

- Walmart does not appear in **Settings → Data sync → Add receipts**, and
  `omsync receipts fetch walmart` is removed.
- Saved Walmart order JSON files under `receipts/WALMART/<provider>/` still parse, match, triage,
  and apply through `WalmartVendor` (`omsync receipts run walmart` and Reconcile).
- Supporting Walmart receipt acquisition in the future requires a non-CDP mechanism, such as
  importing saved or exported order files, instead of Chrome remote debugging.
