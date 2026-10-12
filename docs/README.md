# omsync documentation

Architecture pages define omsync's intended behavior and explain consequential design
choices. Code expresses implementation. Operating guides explain how to use the product.

## Foundations

- [System](architecture/system.md) — purpose, scope, security boundary, and technical design.
- [Ledger](architecture/ledger.md) — source evidence, identities, money, categories, and links.
- [Changes](architecture/changes.md) — previews, atomic batches, idempotency, and guarded undo.

## Access and interface

- [Access and vault](architecture/access.md)
- [Interface](architecture/interface.md)
- [Settings](architecture/settings.md)

## Transaction work

- [Statement import](architecture/import.md)
- [Sync and connectors](architecture/sync.md)
- [Receipts](architecture/receipts.md)
- [Transactions](architecture/transactions.md)
- [Categories and keywords](architecture/categories.md)
- [Reconciliation](architecture/reconciliation.md)

## Reading and planning

- [Accounts](architecture/accounts.md)
- [Dashboard](architecture/dashboard.md)
- [Reports and exports](architecture/reports.md)
- [Budgets](architecture/budgets.md)
- [Goals](architecture/goals.md)
- [Recurring series](architecture/recurring.md)
- [Backups and recovery](architecture/backups.md)

## Code to architecture

| Package | Architecture pages |
| --- | --- |
| `core/` | [system](architecture/system.md); `batches.py` [changes](architecture/changes.md); `vault.py`, `sessions.py` [access](architecture/access.md); `settings.py` [settings](architecture/settings.md); `backups.py`, `recovery.py` [backups](architecture/backups.md) |
| `ingest/` | [import](architecture/import.md); `sync.py`, `providers.py`, `connectors/` [sync](architecture/sync.md) |
| `ledger/` | [ledger](architecture/ledger.md), [transactions](architecture/transactions.md), [categories](architecture/categories.md), [accounts](architecture/accounts.md) |
| `reconcile/` | [reconciliation](architecture/reconciliation.md) |
| `receipts/` | [receipts](architecture/receipts.md) |
| `plan/` | [budgets](architecture/budgets.md), [goals](architecture/goals.md), [recurring](architecture/recurring.md) |
| `reports/` | [dashboard](architecture/dashboard.md), [reports and exports](architecture/reports.md) |
| `web/` | [interface](architecture/interface.md), plus the page of the feature a view serves |
| `cli/` | the page of the feature a command serves |

## Operating guides

- [Import and review statements](how-to/import-statements.md)
- [Run sync and obtain receipts](how-to/sync-and-receipts.md)
- [Back up and restore](how-to/back-up-and-restore.md)

## Architecture decisions

- [Separate Chase personal and Chase Business connectors](adr/0001-separate-chase-connectors.md)

For installation and startup, see the [project README](../README.md). Engineering instructions
are in [AGENTS.md](../AGENTS.md); review standards are in [CODING_STANDARDS.md](../CODING_STANDARDS.md).
