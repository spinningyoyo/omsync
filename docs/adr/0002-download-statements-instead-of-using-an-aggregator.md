---
status: accepted
---

# Download statements from bank websites instead of using an aggregator

## Context

omsync needs each account's transactions. Two methods are possible:

- **An aggregator**, such as Plaid or SimpleFIN. The aggregator logs in to the banks and sends the
  transactions through its API.
- **The bank websites.** omsync logs in to each bank in Chrome and downloads the statement files.

maneko first used a Plaid developer account. Plaid then stopped that access, and maneko stopped
syncing. maneko then downloaded statements from the bank websites. These scrapers work today.

An aggregator sets its own prices, plans, and terms, and it can change them at any time. Plaid
also needs an app that the internet can reach over HTTPS. omsync listens only on this Mac.

Statement files contain the statement balances. omsync shows each balance as the bank reported it.

## Decision

Download statements from the bank websites, with the maneko scrapers. Do not use an aggregator.

## Consequences

- No third party can stop omsync from syncing.
- omsync has no API costs.
- The bank data goes only from the bank to this Mac.
- A bank can change its website and stop a scraper. A change to a scraper needs a live test at
  that bank.
- A sync opens Chrome and can ask for a code from the bank. A sync does not run in the background.
- Each new bank needs a new scraper.
