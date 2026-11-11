# omsync documentation

Good code explains itself. These pages are for people. Each page shows a feature with diagrams and
lists the design decisions that the code cannot show. Each page has the same sections: Purpose,
Context, Flow, Design decisions, and Terms.

## Foundations

- [System](architecture/system.md): scope, boundaries, and code layers.
- [Ledger](architecture/ledger.md): the data model, evidence, and who sets a category.
- [Changes, undo, and backups](architecture/changes.md): batches, undo, backup, and restore.
- [Access and vault](architecture/access.md): sign-in and the Bitwarden vault.
- [Interface and settings](architecture/interface.md): pages, edits, and where each setting
  lives.

## Getting transactions

- [Statement import](architecture/import.md)
- [Sync, providers, and connectors](architecture/sync.md)
- [Bank requests](architecture/bank-requests/README.md): [Bank of America](architecture/bank-requests/boa.md),
  [Chase](architecture/bank-requests/chase.md), [Citi](architecture/bank-requests/citi.md), and
  [Fidelity Rewards](architecture/bank-requests/fidelity.md).
- [Receipts](architecture/receipts.md)

## Explaining transactions

- [Transactions](architecture/transactions.md)
- [Categories, keywords, and rules](architecture/categories.md)
- [Contacts and counterparties](architecture/contacts.md)
- [Reconciliation](architecture/reconciliation.md)

## Reading and planning

- [Accounts](architecture/accounts.md)
- [Home dashboard](architecture/dashboard.md)
- [Reports and exports](architecture/reports.md)
- [Plan: budgets and goals](architecture/plan.md)
- [Recurring series](architecture/recurring.md)

## Code to pages

| Package | Pages |
| --- | --- |
| `core/` | [system](architecture/system.md), [changes](architecture/changes.md), [access](architecture/access.md), [interface](architecture/interface.md) (settings) |
| `demo/` | [access](architecture/access.md), [system](architecture/system.md) |
| `ingest/` | [import](architecture/import.md), [sync](architecture/sync.md), [bank requests](architecture/bank-requests/README.md) |
| `ledger/` | [ledger](architecture/ledger.md), [transactions](architecture/transactions.md), [categories](architecture/categories.md), [accounts](architecture/accounts.md), [contacts](architecture/contacts.md) |
| `reconcile/` | [reconciliation](architecture/reconciliation.md) |
| `receipts/` | [receipts](architecture/receipts.md) |
| `plan/` | [plan](architecture/plan.md), [recurring](architecture/recurring.md) |
| `reports/` | [dashboard](architecture/dashboard.md), [reports](architecture/reports.md) |
| `web/`, `cli/` | [interface](architecture/interface.md), and the page of the feature |

## Operating guides

- [Import and review statements](how-to/import-statements.md)
- [Run sync and obtain receipts](how-to/sync-and-receipts.md)
- [Back up and restore](how-to/back-up-and-restore.md)
- In the app: Settings → Guides. The text is in
  [templates/guides](../src/omsync/web/templates/guides/).

## Architecture decisions

- [Build omsync instead of using Sure](adr/0001-build-omsync-instead-of-using-sure.md)
- [Download statements from bank websites instead of using an aggregator](adr/0002-download-statements-instead-of-using-an-aggregator.md)

To install and start omsync, see the [project README](../README.md). Engineering instructions are in
[AGENTS.md](../AGENTS.md). Review standards are in [CODING_STANDARDS.md](../CODING_STANDARDS.md).
