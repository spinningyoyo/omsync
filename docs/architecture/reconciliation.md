# Reconciliation

## Purpose

Reconciliation links each transaction to its counterpart: the other half of a transfer, a card
payment, a refund, or a joint bill. A linked transfer is not spending or income.

## Context

```mermaid
flowchart LR
    Import[Import and sync] --> Counterparty[Counterparty] --> Categories[Categories]
    Categories --> Reconciliation[Reconciliation]
    Reconciliation --> Links[(Links)]
    Links --> Reports[Reports]
    Links --> Contacts[Contact balances]
```

## Flow

### Each run

```mermaid
flowchart LR
    Import[Import or sync] --> Identify[Identify the counterparty]
    Identify --> Categorize[Categorize]
    Categorize --> Rules[Run the rules on all history]
    Rules --> Auto[Apply automatic links as one batch]
    Rules --> Review[Review and manual proposals]
    Rules --> NotMatched[Not matched]
    class Review,NotMatched waits
    classDef waits fill:#fde68a,stroke:#b45309,stroke-width:2px,color:#1f1f1f
```

The rules read the counterparty and the category, so those two steps run first.

### Rule order

Specific rules run first. Each rule takes its transactions out of the pool for the next rules.

```mermaid
flowchart LR
    R[Bank reversals] --> J[Joint card bills] --> T[Transfers and card payments]
    T --> P[Round trips with a person] --> U[Round trips through an untracked account]
    U --> S[Store refunds]
```

A reversal runs first because the original also reads as a transfer. A joint bill runs before
transfers so a generic rule cannot take your half of the bill.

### What each transaction becomes

```mermaid
flowchart TD
    Start([A transaction]) --> Linked{Does a link cover it?}
    Linked -->|Yes| Reconciled([Reconciled: a transfer, not spending])
    Linked -->|No| Proposed{Does a proposal include it?}
    Proposed -->|Automatic| Auto([Linked at the next apply])
    Proposed -->|Review or manual| Queue([In a queue])
    Proposed -->|No| Expected{Does its wording need a counterpart?}
    Expected -->|No| Done([Nothing to do])
    Expected -->|Yes| Known{Is the counterparty known?}
    Known -->|Yes| Explained([Explained: no link needed])
    Known -->|No| NM([Not matched])
    class Queue,NM waits
    classDef waits fill:#fde68a,stroke:#b45309,stroke-width:2px,color:#1f1f1f
```

### Joint card bills

You and the co-owner each pay half of a joint card bill into joint checking. The card payment is
evidence, not a side of the link.

```mermaid
flowchart LR
    You[Your account] -->|Your half: linked| Joint[Joint checking]
    Partner[Partner's account] -->|Partner's half: never linked| Joint
    Joint -->|Card payment| Card[Joint card]
    Card -. bill .-> You
```

## Design decisions

- **A link applies automatically only when each side has one possible partner.** An incorrect link
  can hide real spending, so an equal amount is not enough. With two possible partners, omsync makes
  a proposal for review.
- **A removed link stays removed.** omsync records each pair that you unlink or reject, and does not
  propose it again.
- **Only a round trip links to a person.** That means money out to a person and back from the same
  person. The category tells what any other payment from a person repaid.

## Terms

- **Counterpart:** the other half of a pair, such as the arriving side of a transfer.
- **Counterparty:** who the money moved with. See [contacts](contacts.md).
- **Proposal:** a link that waits for a decision.
- **Automatic, review, manual:** the three queues. Automatic applies alone. Review needs a yes or
  no. Manual needs you to pick between candidates.
- **Not matched:** needs a counterpart, has no link, and its counterparty is unknown.
- **Explained:** needs a counterpart, but its counterparty is known, so no link is needed.
