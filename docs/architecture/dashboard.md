# Home dashboard

## Purpose

Home shows your cash flow and what needs doing. It uses the report numbers. It adds no new
accounting.

## Context

```mermaid
flowchart LR
    Counted[Report counted amounts] --> Period[Cash flow, spending trend, outflows]
    Views[The query of each linked page] --> Attention[Needs attention]
    Recurring --> Upcoming[Upcoming]
    Plan[Plan for this month] --> Cards[Budget and goals]
    Period --> Home
    Attention --> Home
    Upcoming --> Home
    Cards --> Home
```

## Flow

### Needs attention

```mermaid
flowchart LR
    Item[Each kind of item] --> Query[Count with the query of the page it opens]
    Query --> Zero{Zero?}
    Zero -->|Yes| Hide[Hide the row]
    Zero -->|No| Row[Show the count and link to that page]
```

The kinds are import reviews, reconciliation to resolve, uncategorized transactions, recurring
items, and sync problems.

## Design decisions

- **Each count uses the query of the page it opens.** The count and the list always agree.
- **Upcoming amounts are labeled as expected.** They are not added to period totals, reports, or
  balances.

## Terms

- **Period sections:** cash flow, spending trend, and outflows.
- **Other:** a group of small categories, not a category of its own.
