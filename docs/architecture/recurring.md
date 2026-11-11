# Recurring series

## Purpose

Recurring shows what is due and what is wrong: bills, subscriptions, and paychecks on a schedule.

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

- **Never say something false about money.** A false Missed or a false bill is worse than a
  missing one. Doubt becomes a question, never a state.
- **Store only your decisions.** omsync derives everything else from the ledger on each read. An
  import, undo, or edit cannot leave the page out of step.
- **You confirm every series.** Detection only suggests. A false bill would inflate Left to pay.
- **Ask each question once.** omsync stores each answer.
- **An amount has an effective date.** A price change never turns past months into Missed.
- **A price change is a question.** Only you know if it is the same bill at a new price.
- **Transfers are not in totals.** A card autopay moves money that the card's purchases already
  count.
- **Upcoming is not ledger money.** It never changes reports or balances.

## Terms

- **Series:** a confirmed repeated bill or income.
- **Occurrence:** one due date of a series: paid, due, or missed.
- **Paid elsewhere:** a due date paid from an account omsync does not track.
