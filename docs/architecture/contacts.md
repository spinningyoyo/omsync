# Contacts

## Purpose and design philosophy

Every bank line has another side: the person, business, or account the money moved to or from. A
**contact** is a person or business that omsync knows by name, and the **owner** is the user. The
[decision record](../adr/0002-identify-the-other-side.md) explains why the other side is modelled
this way. This page describes contacts, the owner, names and the forms they are recognized in,
contact accounts, account nicknames, the other side that omsync recognizes on each transaction or
that you set, the balance with each contact, and the Contacts page that shows them.

## Contacts

A contact has a kind (person or business), a role, and a display name. Display names are unique
without regard to case. The role is the owner, the co-owner of the joint accounts, or neither. At
most one contact is the owner; the database refuses a second. The co-owner role marks the other
owner of the joint accounts. Reconciliation leaves payments with the owner or the co-owner out of
the round-trip rules (see [reconciliation](reconciliation.md)).

A contact may have any of the following, and removing the contact removes them with it:

- **Names**, in parts. A person's name is a given name, an optional middle name, and an optional
  family name. A business, or a name of one word, has only a given name. A middle name needs a
  family name. A contact has at most ten names, and a name already held in another case, spacing, or
  with periods is refused.
- **Patterns**, which recognize wordings no name covers. A pattern is a case-insensitive regular
  expression, saved for payments the user sent (**out**) or received (**in**): valid, at most 200
  characters, and unable to match empty text. A contact has at most ten patterns on a side. A
  pattern may be tied to one of the contact's accounts, such as the owner's untracked brokerage by
  the name in its text.
- **Accounts** outside the ledger, each with a label and optionally last four digits: one of the
  owner's untracked accounts, or a contact's own. A description that names the digits identifies
  the account.

Every change to a contact is a [batch](changes.md): previewed, applied once, and undone while
nothing it changed has changed since. Deleting a contact deletes its names, patterns, and accounts
in the same batch, and undo restores all of them. The same batch clears the other side that
transactions and splits stored for the contact. Removing an account clears it from those that named
it and unties the patterns tied to it.

## Names and their forms

Banks print a name in several ways, and relatives share a family name, so omsync stores a name in
parts and derives the forms. With given name G, family name F, and middle name M, and lowercase g,
f, and m for their initials, a name is recognized as:

| With a family name | With a middle name as well |
| --- | --- |
| G F, F G | G M F, F G M |
| g F, F g | G m F, F G m |
| G f | |

Forms are lowercase, single-spaced, and without periods, so "J." and "J" read alike. Every form
holds the given name or its initial, so a relative who goes by the owner's middle name is not
mistaken for the owner. A name without a family name has one form, the given name as written.

A description names a contact when a run of its words, whole words only, equals one of the contact's
forms. When the forms of different contacts match overlapping words, the longest match wins: with
the owner Alex Jamie Example and a relative Jamie Example, "ALEX J EXAMPLE" is the owner and "JAMIE
EXAMPLE" is the relative. Overlapping matches of equal length for different contacts identify
nobody.

## The owner

The owner is the contact for the user. Reconciliation reads the owner's names in all their forms,
and also as the user typed them, as the user's own names in transfer wordings: a Zelle payment to or
from one of them, the account-holder field of a transfer, and the joint rule's test of whose
contribution an inflow is (see [reconciliation](reconciliation.md)). The first name added on the
Contacts page creates the owner, who is then a contact like any other, with a page of its own.

## Account nicknames

A **nickname** is the name a payment app shows for a tracked account, such as the name on a Zelle
payment that moves money between two of the user's accounts. It belongs to the account, and is
recognized in the same wordings as the owner's names. It is edited on the account's page (see
[accounts](accounts.md)). It is not an **alias**, which maps a statement file's account ID to an
account at import (see [accounts](accounts.md)). An account has at most ten nicknames, unique
without regard to case, and they go with the account if it is deleted.

## The other side

The **other side** of a transaction is the contact, and optionally one of the contact's accounts,
that the money moved to or from. It is stored on the transaction, and on a split, with an origin:
**none**, **recognized** by omsync, or **manual**, the user's choice. A manual choice outranks
recognition, which never replaces it and never sets a split.

After every import and sync omsync recognizes the other side of every transaction whose origin is
not manual, from its statement description, by the first of these that finds anybody:

1. A contact account named by its last four digits, or matched by a pattern tied to it: that
   contact and account. Digits that belong to a tracked account never name a contact account.
2. A payment that names a person, where a saved contact's name is in the name part: that contact.
   The wordings are the Zelle ones [reconciliation](reconciliation.md) reads, "payment to" and
   "payment from" among them, and those that put the name after the confirmation: "Zelle Transfer
   Conf# ABC123; SAM FRIEND", and for money in Bank of America's "Online Banking Transfer Conf#
   ABC123; FRIEND SAM" from another of its customers. A memo after "for" is not part of the name.
   Reading a wording this way never makes its transactions expected to have a counterpart.
3. A contact's pattern on the side the sign names: that contact.
4. A **pass-through**: a transfer to an account of the owner's, of exactly the amount that one other
   contact paid in during the seven days before or after it, the same day included, where neither
   the payment nor the transfer has another such partner. The contact may fund the transfer first
   or reimburse it afterwards. The window is counted once for both directions: a second payment of
   that amount on either side makes the transfer ambiguous. The money is the contact's, so the
   transfer's other side is the contact. A transfer to the same account with no such payment near it
   stays the owner's.

When a step finds more than one contact or account, nobody is identified, and no later step
guesses. A payment that names only the owner has no account, tracked or not, so nothing is stored
for it. Recognition that no longer finds anybody returns the transaction to none, and it reruns
after each import. It is one [batch](changes.md), previewed and undone like any other. A
payment from a person with no saved contact stores nothing either; reconciliation still reads the
name (see [Not matched](reconciliation.md#not-matched-and-the-other-side)).

## Balances

Every contact whose role is neither owner nor co-owner has a **balance**: what is owed between you
and that contact. The owner's lines are your own accounts, and the owners keep no balance between
them. Balances are read-only. They create no links, change no transaction, and do not change
reconciled state, which stays derived from links between tracked accounts.

A contact's **lines** are its transactions without splits and its splits whose category type is
Owed, such as a loan: money that changes what one side owes the other. A transfer is between your
own accounts, so it is not a line. A split belongs to the contact it names, or, naming none, to the
contact of its transaction, and its
category is its own. Spending and income lines are settled when paid, so they never count, and
neither do uncategorized ones.

A line counts the amount [reports](reports.md#counted-amounts) count, with one difference. Joint
share halves a joint account's amount once per transaction, as reports do, and splits share the
halved amount in proportion; with joint share off a line counts in full. Links never reduce a line,
so the two payments of a round trip cancel each other by clearing.

Money out to a contact (a negative amount) is what the contact owes you, and money in from them
reduces it. The balance is the negated sum of the counted amounts. Positive, the contact owes you.
Negative, you hold money for the contact or owe it.

**Clearing.** Lines are walked oldest first, by posting date, then transaction, then split position.
Each line first clears the open items of the opposite direction, oldest first, and partly when the
amounts differ. What remains of the line opens as an **open item** of its own, with its counted
amount and its uncleared amount. At most one direction has open items afterward, and they add up to
the balance. Several loans repaid by one payment, one loan repaid in parts, and a late repayment all
clear without a pairing rule or a window. A reimbursement with nothing owed against it stays an open
item, money you hold for the contact, until the contact's share of the original purchase is
recorded as a split that names the contact.

The balance view of a contact has the balance, its direction (the contact owes you, you owe the
contact, or settled), the open items, and every line with how much of it cleared. The list of
balances holds the contacts with a balance other than zero, the largest first and by name among
equals. The joint-share preference is read where the balance is shown and passed in.

A balance is as complete as its attribution: money passed through an untracked account counts for a
contact only when a pass-through, a pattern, or you name the contact.

## Setting the other side

You can set the other side of a transaction, of several transactions at once, or of one split, to a
contact, to one of a contact's accounts, or to one of your own accounts outside the ledger. The
origin is **manual**, so recognition never replaces it. Clearing returns the origin to none, and
recognition may then set the other side again. A split with no other side of its own belongs to its
transaction's contact, as balances count it. A setting is one [batch](changes.md), refused if the
transaction changed since you saw it, and undone like any other. Saving a split again keeps the other
side of the part at each position.

Where you set it: the detail page of a transaction, which also shows what is there and whether omsync
recognized it or you set it (see [transactions](transactions.md)), and the Not matched list of
[reconciliation](reconciliation.md#not-matched-and-the-other-side), where several rows are set at once
and the contact may be created in the same step. A contact made there has a name read as written: the
first word is the given name and the last the family name.

## Merging and deleting

Merging folds one contact into another in one batch: the merged contact's names, patterns, accounts,
and the other side of its transactions and splits move to the kept contact, which keeps its own name
and role, and the merged contact is deleted. A name or pattern the kept contact already has is not
repeated. You cannot be merged into anyone, the co-owner can be merged only into another co-owner,
and a merge that would give the kept contact more names, patterns, or accounts than a contact may
hold is refused. Deleting a contact clears the other side that its transactions and splits stored.
Both are undone while nothing they changed has changed since.

## Running it automatically

The step that follows an import (see [reconciliation](reconciliation.md)) identifies the other side
of every transaction, then [categorizes](categories.md) with the keywords, then links the proposals
that need no review. Recognition comes first because a rule that names a person, a contact, or your
accounts reads the other side, and recognition reads no category, so it loses nothing by going first. The step
also runs when the server starts, after its database upgrade and before the first page, and after
every change you make to contacts, names, patterns, accounts, nicknames, or the other side of a
transaction. Each part of it is a batch of its own, labelled automatic, and the confirmation says
what your change did and how many transactions the step identified, categorized, or linked. A
failure of the step is reported and never undoes your change or stops the server. Undoing your change runs the step again. The step's batches
record the batch of your change that triggered them, and undoing your change from its confirmation
undoes them first, newest first, and then the change, as one undo: if later work has touched any of
the rows, none of it is undone.

## Settings

The Contacts page in [Settings](settings.md) lists every contact, you first, with its kind, role,
balance and direction, and number of open items; the rest follow, the largest balance first and by
name among equals. Each contact has a page of its own, yours included. A contact's page has its
balance with the open items and every line with how much of it cleared, each linking to its
transaction, and its names, patterns, accounts, role (contact or co-owner), merge, and delete. Your
page has no balance, role, merge, or delete. It has your names, your accounts outside the ledger
with their patterns, and how many transactions each of your names matches in payee text. An
account's nicknames are edited on the account's page.

## Migration

Saved names, formerly a list of literal strings, became the owner's names. The longest saved name of
two or more words is split into given, middle, and family names; every other saved name that its
forms do not already cover is kept whole, so each saved name is recognized as before.

Counterparties became contacts of the same kind, parsed into names as the owner's was, with their
out and in patterns as the contact's patterns. A counterparty named like an existing contact added
its patterns to that contact. The "no match expected" mark has no successor: its payments are
explained by their other side once it is recognized, and recognition runs after the next import.
Acknowledgments of single transactions were removed; the upgrade refuses to run while any remain.

