---
status: accepted
---

# Identify the other side of every transaction

## Context

Reconciliation asks whether a transaction should have a counterpart, and lists the ones that lack
one as Not matched. Several mechanisms answer that question, each covering what the others miss:
counterparties with payee patterns that may be marked "no match expected", a list of the user's
own names added to the transfer patterns, partner contributions labeled by the joint rule,
forwards computed on each run, and acknowledgments of single transactions.

Each explains a gap by saying no match is expected, without saying why. The engine cannot record
the plain fact that the other side of a payment is known and lies outside the ledger, so payments
from people stay Not matched although their descriptions name the sender. Saved names are literal
strings that stand in for different things: an ACH account-holder field, a payment-app nickname of
a tracked account, a bank's wording for an incoming transfer. A category per relationship was
tried for the same purpose and removed, because a category says what money was for, not who is on
the other side.

## Decision

Model the question accountants ask of every bank line: **what is on the other side?** Each
transaction's other side is one of:

1. **A tracked account.** The money moved between two accounts in the ledger, so a link must
   explain it. The transfer category is a clearing account: it nets to zero when every such
   transaction is linked. This is the only case where a missing link is an error.
2. **An untracked account of the user's**, such as a brokerage or credit union account the user
   chooses not to sync. It is recorded as the user's, with no balance and no statements, and its
   category says how the money counts. No link is expected.
3. **A contact**: a person or business with its own accounts outside the ledger. No link is
   expected.
4. **A merchant**, for purchases and refunds. No link is expected.
5. **Unknown.** This, together with case 1 left unlinked, is Not matched.

The question is asked of the transactions that are asked today, those whose wording makes them one
half of a pair (see [reconciliation](../architecture/reconciliation.md)). That set does not change;
a known other side now answers it.

### Contacts and the owner

A **contact** replaces the counterparty. It has a person or business kind, one or more names, the
patterns that recognize wordings no name covers, and any accounts of its own that descriptions name
by last four digits.

The user is the **owner** contact. The owner's name is stored as given, middle, and family names
and recognized in the forms banks print: given and family name in either order, with or without
the middle name or its initial, and with the given name as an initial. Every form includes the
given name or its initial, because relatives share the family name and one may go by the owner's
middle name. The owner's untracked accounts belong to the owner. A payment-app nickname that names
a tracked account is a **nickname** of that account, not a name, distinct from the statement
aliases that identify an account at import.

A contact may be the **co-owner of the joint accounts**. Joint accounts are a joint operation: each
owner counts its share, which joint share already applies. The co-owner's deposits into a joint
account are a co-owner's contribution, identified by the co-owner's names, and no balance between
the owners is kept.

### Recognizing the other side

The other side is resolved in this order, each step deterministic:

1. An existing link, or a proposal of the transfer rules: a tracked account.
2. A description naming an account of a contact or of the owner by its last four digits, or a
   tracked account by its nickname.
3. A payment naming a person, in any of the payment wordings banks use, including those that put
   the name after the confirmation: the contact with that name, or, with none saved, the named
   person, which is still an identified other side.
4. A contact's patterns.
5. A **pass-through**: a transfer to an untracked account of the owner's of exactly the amount a
   contact paid in during the seven days before or after, where neither the payment nor the
   transfer has another such partner. The money is the contact's, so the transfer's other side is
   the contact. This is specific identification: the same amount, in and then out, or out and then
   reimbursed, is the evidence. A transfer to the same account with no such payment near it is the
   owner's until the user says otherwise, in whole or by a split.

The result is stored on the transaction with an origin, as a category is: the user's choice
outranks recognition, and recognition never replaces it. A split may have its own other side, so
one purchase can record that part of it is owed by a contact. Not matched asks one question, "what
is on the other side?", and each answer is a durable fact about an account or a contact.

Classification stays separate: an uncategorized payment belongs to the Uncategorized list, since
what money was for and who sent it are different questions.

### Contact balances

Every contact except the owner and a co-owner has a **balance**: what is owed between the user and
that contact, as in an accountant's sub-ledger per customer or supplier.

- **What moves it.** The contact's transactions and splits whose category type is Owed, such as a
  loan: money that changes what one side owes the other. Only money the user classified as owed
  counts, out of prudence, since a transfer is between the user's own accounts and moves no balance.
  Spending and income with a contact are settled when paid. Amounts count as reports count them,
  joint share included.
- **Open items.** Money out to the contact and money in from them clear each other oldest first.
  What remains uncleared are the open items, each with its uncleared amount and date. Several
  loans repaid by one payment, one loan repaid in parts, and a late repayment all clear without a
  pairing rule, a window, or a guess.
- **What it shows.** The balance, its direction (the contact owes the user, or the user holds
  money for the contact), and the open items. A reimbursement with nothing owed against it is an
  open credit until the contact's share of the original purchase is recorded as a split.
- **What it does not do.** It creates no links, changes no transaction, and does not change
  reconciled state, which stays derived from links between tracked accounts.

### What is removed

- Counterparties, "no match expected", the counterparty rule, and counterparty acknowledgments.
  Saved counterparties become contacts.
- The saved-names preference. Its entries become the owner's names and account nicknames.
- Partner-contribution labeling and forward detection. Both explained transactions whose other
  side is now identified directly.
- Acknowledgments of single transactions. Setting the other side replaces them.

## Considered alternatives

### A category per relationship

Rejected. A category answers what money was for. Encoding who is on the other side forces one
category per person and misstates reports.

### A per-category flag "expects a matching transaction"

Rejected. Whether a link is expected depends on where the other side's account is, not on what the
money was for: a loan to a friend expects none, a loan moved through a tracked account does.

### Keep counterparties and add acknowledgment forms

Rejected. An acknowledgment explains a gap without saying why, so the model cannot tell a missing
transfer from money that left the ledger on purpose.

### Allocate pass-through transfers from a held balance

Rejected. Treating any transfer as drawn first from money held for a contact settles that
contact's balance by construction and hides money the user sent for their own purposes.

### Pair a contact's payments one to one

Rejected. Pairing rules need windows, miss split and late repayments, and must refuse ambiguity.
Open-item clearing answers what is still owed without choosing pairs.

## Consequences

- Not matched means a transfer whose tracked counterpart is missing, or a transaction whose other
  side is unknown.
- Explained transactions need no reason such as "no match expected": the other side is the reason.
- The joint rule tells the owner's contributions from the co-owner's by their names.
- Ledger, reconciliation, settings, and categories pages adopt the vocabulary: other side, contact,
  owner, co-owner, untracked account, balance, open item.
- Migrations convert counterparties and saved names, and store the recognized other side of
  existing transactions.
- The link rules for transfers, refunds, round trips, and joint settlements are unchanged. A round
  trip's two payments both belong to the contact, so they cancel in its balance.
- A balance is as complete as its attribution. Money passed through an untracked account counts
  for a contact only when a pass-through, a pattern, or the user names the contact.

## Plan

1. Contacts with kinds, roles, names, patterns, and accounts; the owner and account nicknames,
   replacing saved names.
2. The other side on transactions and splits, with origin, set by recognition after import and by
   the user. Migrate counterparties to contacts. Not matched becomes "unknown, or tracked and
   unlinked". Remove counterparty matching, forwards, partner labeling, and acknowledgments.
3. Contact balances with open-item clearing.
4. One Contacts page: names, roles, accounts, balance, open items, and transactions. Not matched
   offers "what is on the other side?" for several rows at once.
