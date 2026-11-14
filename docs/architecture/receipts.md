# Receipts

## Purpose

A receipt says what a purchase or refund contained. omsync uses Amazon and Costco receipts to split
a charge into categories. A receipt never creates money.

## Context

```mermaid
flowchart LR
    Files[Amazon and Costco files] --> Receipts
    Keywords[Vendor keyword lists] --> Receipts
    Ledger[(Ledger)] --> Receipts
    Receipts -->|category or splits, receipt link| Ledger
```

## Flow

```mermaid
flowchart TD
    Files[Receipt files] --> Facts[Read items and charges]
    Facts --> Payer{Paid by a tracked card?}
    Payer -->|Paid elsewhere| Skip[Skip]
    Payer -->|Unknown| Review[Review only]
    Payer -->|Yes| Match{One transaction fits?}
    Match -->|No or several| Review
    Match -->|Yes| Items[Categorize items with the vendor's keywords]
    Items --> Apply[Apply category or splits as one batch]
    class Review waits
    classDef waits fill:#fde68a,stroke:#b45309,stroke-width:2px,color:#1f1f1f
```

## Design decisions

- **A receipt applies only to one matching transaction.** Amount, sign, payee, card, and date window
  must match exactly one transaction. If more than one matches, the receipt waits for review.
- **A receipt with an unknown card does not apply automatically.** Amount and date cannot show that
  the purchase is yours.
- **The period limits what applies, not the evidence.** A shorter period cannot make an ambiguous
  match unique.
- **Amazon: an order total must equal one exact set of charges.** A first-fit guess can take a
  different purchase. An order with a gift card waits for review, because Amazon does not show the
  gift card amount.
- **Costco uses your signed-in browser.** omsync does not use a vault login for Costco.

## Terms

- **Charge:** the amount of a receipt that one transaction on one account paid.
- **Receipt match:** the link from a charge to its transaction.
- **Vendor login:** one sign-in's receipts at a vendor.

See [get receipts](../how-to/get-receipts.md) for the steps.
