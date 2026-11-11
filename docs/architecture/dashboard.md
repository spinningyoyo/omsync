# Home dashboard

## Purpose

Home shows your cash flow and what needs doing. It uses the report numbers. It adds no new
accounting.

## Context

```mermaid
flowchart LR
    Counted[Report counted amounts] --> Period[Cash flow, spending trend, outflows]
    Views[Each destination view's query] --> Attention[Needs attention]
    Recurring --> Upcoming[Upcoming: next 14 days]
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
items, balance check failures, and sync problems.

## Design decisions

- **Each count uses its page's own query.** A second definition would let the count and the list
  drift apart.
- **Period sections follow the period picker. The others follow today.** Each header says which.
- **Upcoming is expected money.** It is labelled as expected and never added to period figures.
- **One color for each category on the page.** The donut and the cash-flow diagram read together.
- **Figures go with every chart.** A chart's shape is never the only source of a number.
- **Layout is a preference.** Order, width, height, and collapse save directly, not as batches.
- **Sections keep one reading order.** A gap stays a gap, so the keyboard, a screen reader, and
  the eye agree.

## Terms

- **Period sections:** cash flow, spending trend, and outflows.
- **Other:** a group of small categories, not a category of its own.
