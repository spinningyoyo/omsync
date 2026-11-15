# Recurring series

## Purpose

Recurring tracks bills, subscriptions, and paychecks that repeat on a schedule. It shows what is
due, paid, or missed.

## Context

```mermaid
flowchart LR
    Ledger[(Transactions)] --> Detect[Detection: on every read]
    Detect --> Suggest[Suggestions]
    Decisions[(Your decisions)] --> Derive[Matches and occurrences: on every read]
    Ledger --> Derive
    Derive --> Overview
    Derive --> Calendar
    Derive --> Home[Home: Upcoming]
```

## Flow

### Suggestion to series

```mermaid
flowchart TD
    Suggest[Suggestion] --> Decide{You decide}
    Decide -->|Add bill| Series[(Series)]
    Decide -->|Not recurring| Dismissed[(Dismissed)]
    Decide -->|Skip| Visit[Only for this visit]
    class Decide waits
    classDef waits fill:#fde68a,stroke:#b45309,stroke-width:2px,color:#1f1f1f
```

### One occurrence

```mermaid
stateDiagram-v2
    [*] --> Due
    Due --> Paid: a matching charge, or marked paid elsewhere
    Due --> NewAmount: only one charge, outside the band
    NewAmount --> Paid: accept the new amount
    NewAmount --> Due: not this bill
    Due --> Missed: window closes with no charge
```

## Design decisions

- **Detection only suggests a series. You confirm it.** An uncertain charge, such as a new price,
  becomes a question, not a Missed state.
- **omsync stores only your decisions.** It calculates everything else from the ledger at each
  read, so the page stays correct after an import, an undo, or an edit.
- **An amount has an effective date.** A price change does not make past months Missed.
- **Totals leave out transfers.** A card autopay moves money that the card purchases already count.

## Terms

- **Series:** a confirmed repeated bill or income.
- **Occurrence:** one due date of a series: paid, due, or missed.
- **Paid elsewhere:** a due date paid from an account omsync does not track.
