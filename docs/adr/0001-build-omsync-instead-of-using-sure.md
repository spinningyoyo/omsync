---
status: accepted
---

# Build omsync instead of using Sure

## Context

One household uses omsync. Its workflow needs these functions:

- Match transfers between its own accounts, including joint accounts.
- Count joint-account money at half.
- Keep loan balances with each contact.
- Split Amazon and Costco charges by receipt item.
- Download statements from bank websites, with logins from Bitwarden.
- Keep all data on one Mac.

maneko, the earlier tool, does most of this. Its scrapers, matching rules, and categories work on
real data. maneko is written in Python, and the owner prefers Python.

Sure is an open-source app near this scope. It is a Ruby on Rails app with PostgreSQL and Redis.
It needs changes in Ruby to fit this workflow.

## Decision

Build omsync in Python, from the maneko code.

Use other products as references:

- **Sure** (AGPL-3.0): the visual design, the page layout, the reports, and the bank-wording
  reader. omsync copies this code.
- **Monarch**: Flex budgets (fixed, flexible, and non-monthly). omsync uses the design only.

## Consequences

- The workflow sets the features. No other product sets them.
- The owner maintains all of the code.
- Copied Sure code makes omsync AGPL-3.0.
- Each new feature starts with a look at how other finance apps do it.
