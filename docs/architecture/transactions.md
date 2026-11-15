# Transactions

## Purpose

The register shows the ledger as rows. You edit transactions there, one at a time or in bulk.

## Context

```mermaid
flowchart LR
    Ledger[(Ledger)] --> Register
    Register -->|memo, category, split| Batch[(Batches)]
    Register --> Detail[Transaction detail: counterparty]
    Register --> Review[Needs review: import decisions]
    Filters[Saved filters] --> Register
```

## Flow

### Bulk change

```mermaid
flowchart LR
    Select[Select rows or all matches] --> Preview[Preview: changed and skipped]
    Preview --> Apply[Apply if still current]
```

## Design decisions

- **Only category, memo, counterparty, and splits are editable.** Date, account, and amount come
  from the statement.
- **A row shows its links, read only.** A link icon opens what reconciliation or a receipt tied
  the transaction to: both transactions of a pair together, or the receipt. Links change on
  Reconcile, not in the register.

## Terms

- **Needs review:** import rows that may be a known transaction. You pick one or add a new one.
- **Saved filter:** a named set of conditions.
- **Category is empty:** no category, or a split that has no category.
