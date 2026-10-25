# Reconciliation

## Purpose and design philosophy

Reconciliation proposes relationships explaining transactions. A false link can hide real
spending or misrepresent a reimbursement, so a false positive costs more than a missed
proposal. Equal amounts are evidence, not proof of a relationship.

A **proposal** is a typed link waiting for a decision, with confidence and evidence. A
**transfer** moves money between tracked accounts. A **P2P payment** involves another person,
whose other account is not in this ledger. A **joint settlement** is the user's contribution
to a joint card bill, not the bill payment itself. A transaction is **not matched** when it should
have a counterpart and nothing accounts for it, its **other side** included; see
[Not matched and the other side](#not-matched-and-the-other-side).

## Behavior and flow

A run reads a date range and writes nothing. It presents transfers, refunds, P2P payments, and joint
settlements in confidence-based queues. The rules also read as far beyond each end of the range as
their widest window, the joint bill's 45 days, and the run keeps the proposals with a side in the
range: a match near an edge meets the same rivals as in a run over the whole history, so it is never
automatic only because a competing transaction posted outside the range. The automatic queue holds
exact, unambiguous matches, so the user is not asked about them. After every [import](import.md) and
[sync](sync.md), omsync [identifies the other side](contacts.md#the-other-side) of each
transaction, because the round-trip rules skip payments to the owner, and [categorizes](categories.md)
with the keywords, because the return and reimbursement rules skip money whose category type is
Excluded, Owed, or Income, such as transfers, loans, investments, and pay. The same step also runs when
the server starts and after changes to contacts (see
[contacts](contacts.md#running-it-automatically)). It then runs reconciliation
over the whole history and applies every automatic-queue proposal as one batch, labelled as
automatic, that undo reverses like any other. If categorizing or identifying fails, nothing is
applied. Nothing is applied when there is nothing to apply. The review and manual queues are never
applied without the user's action: the user can approve, reject, link manually, unlink, or leave
such a proposal unresolved. The Reconcile page can still approve the automatic proposals that appear
between imports, such as after data is edited.

```mermaid
flowchart TD
    Range[Choose the transaction date range] --> Evidence[Read source descriptions, accounts, and links]
    Evidence --> Proposals[Propose explanations with evidence]
    Proposals --> Decision{User decision}
    Decision -->|Approve| Revalidate[Revalidate the selected proposals]
    Revalidate --> Links[Record links in one batch]
    Decision -->|Reject or unlink| Remember[Remember the transactions and amount]
    Decision -->|Uncertain| Leave[Leave unresolved]
    Links --> Rerun[Recompute proposals and coverage]
    Remember --> Rerun
```

Specific relationships are considered before broad similarity, so a joint contribution
cannot be taken by a generic reimbursement rule. A run has deterministic ordering and
reserves proposed explanations without changing ledger data. Fully explained transactions
do not become new candidates; partial links leave an unexplained remainder. Evidence such
as a bill anchor can remain useful even after that transaction has been linked.

Rule text is the statement row's description, read through the same function import uses
(NAME, with MEMO joined only for institutions whose format continues NAME there), never
Transaction.memo. Chase can place confirmation evidence in MEMO alone, and a NAME cut at its
length limit continues in MEMO without a space, so identifiers and account references can be
glued to the words around them.

Evidence shows confirmation identifiers, signs, date gaps, amount differences, account
references, and existing links. An account reference is a description naming the last four
digits of an account, by a mask (`...1111`, `########4321`), an account word (`ACCT# 2222`,
`CHK 3333`), or “to”/“from” followed by the account's name. The digits may be the account's
number or the statement ID its bank shows now. Only “to” and “from” are directional: a
sender's “to” names the destination and a receiver's “from” names the source, and agreement
or contradiction is shown to the reviewer. A reference to another tracked account is also a
transfer signal. It proposes a transfer when that account holds the one equal and opposite
amount within four days, unique from both sides, and no side names a different tracked account
than its partner's. The reference replaces a confirmation number as the signal, never the
amount, date, or uniqueness conditions. When several credits fit one debit and exactly one of
them reads as money from the user's own accounts (a transfer or card-payment wording, the user's
own name, or another tracked account), that credit is the partner: a Zelle payment from someone
else, or an Apple Cash credit, of the same amount is not the transfer the debit names.

## Transfer and refund rationale

A transfer needs a transfer signal as well as compatible signs, amount, account roles, and
posting times. A retail debit must not become a card payment merely because its amount fits.
“Payment” inside a Zelle description is not by itself a credit-card bill payment.

Posting gaps reflect the mechanism: confirmation-based account transfers, same-day Zelle,
card payments, and card credit-balance refunds have different expected timing. A card autopay
can post on the card on a Friday or Saturday and at the bank on Monday, so card payments allow
five days. A shared confirmation does not waive sign, amount, or account checks. Uniqueness
must hold from both sides; multiple fitting partners require a manual decision, unless the
partners are interchangeable or only one reads as the user's own money, as described below.

Banks that give the two sides of one transfer different wordings often print the same long
identifier on both. A number of ten or more digits, or a Zelle confirmation token, held by
exactly two transactions is a **shared ID**. Two transactions on different accounts with a
shared ID, equal and opposite amounts, at most three days apart, are an automatic transfer.
Ten digits keep dates, last fours, and nine-digit routing numbers from being IDs. An ID held by
three or more transactions decides nothing, and neither does a transaction whose two IDs lead to
different partners. Amazon and Costco transactions are left to [receipts](receipts.md).
The ID resolves several same-day transfers of one amount that no amount rule could separate.
Existing confirmation rules stay for their forms and windows; the shared-ID rule runs after them.

Card payments recognize the issuers' current wordings on both sides, including scheduled-payment
and “payment from checking” forms. The same card-credit wordings anchor joint settlements.
Where a wording is ambiguous, an account reference can still resolve the pair.

A bank **reversal** undoes a transaction in the same account: a line starting with REVERSAL and
the original it names. The two share a reference number of six or more digits (a transaction #, a
Conf#), have exactly opposite amounts, and post within ten days of each other. Reversals run
before every other rule, because the original also reads as a transfer and its account reference
would otherwise send it to a partner in another account. It is recorded as a refund, the link kind
that already means money returned to the account it left. One original for one reversal is
automatic, since a shared reference, an exact opposite amount, and one account leave no other
explanation. A reversal with several originals, or an original with several reversals, is a
review proposal of the nearest pair.

A **round trip through an untracked account** is money sent from one account to an account omsync
does not track and back to the same account: two transactions on one account, both reading as
transfers, that name the same last four digits, which belong to no tracked account, with exactly
opposite amounts at most thirty days apart in either order. It is recorded as a refund, like a
reversal. A masked number elsewhere in a description, such as a company ID or the card number on a
purchase, is not an account reference, which is why both sides must read as transfers. A payment
whose stored other side is the owner or the co-owner is left out, as in the P2P round trip, so an
untracked account that is recorded as the owner's is explained by its other side and not offered
here. Every pair is automatic: payments that share an account, an amount, and the other account are
interchangeable, so of several, the nearest pairs are linked first, by date gap and then IDs.

Refunds can return to the purchase account or another account. Exact source evidence is
stronger than brand similarity. Generic PayPal transfer text identifies a mechanism, not
a merchant. Amazon and Costco purchase/refund explanations belong to
[receipts](receipts.md), so generic return rules do not compete for their transactions.
When an allowed cent difference exists, the link explains only the smaller amount and
leaves the remainder visible.

A store refunds; a person does not. A Zelle payment, anything through Venmo, and a PayPal
transfer never take part in a merchant return, on either side, whatever their category. A
same-account return of the exact amount is automatic only when the purchase and the refund
name the same brand. An equal amount alone, such as a baggage fee and a travel credit, goes to
review. Purchases on one account with the same amount and the same description are
interchangeable, so a refund that fits several of only those takes the latest, then the highest
ID, and is automatic on the same brand condition. A refund that fits several different purchases
is a manual decision.

## P2P rationale

A P2P inflow can reimburse spending, repay a loan, contribute to shared finances, or be
unrelated income. omsync does not guess which purchase a payment from a person repaid. The guess
would rest on an equal amount and on whatever words the sender typed, which is too little to be
right most of the time, and a right guess would change nothing: refund and P2P links never change
a report, and the inflow's category already says how it counts. A payment from a person is
linked only by a round trip, below, and otherwise answered by its category and its other side (see
[Not matched](#not-matched-and-the-other-side)).

A **round trip** is money sent to a person that comes back from them, or the reverse: a Zelle
payment to a name and a Zelle payment from the same name, in any tracked accounts, of exactly
equal amounts and at most thirty days apart in either order. The person is the first two name
tokens, ignoring case, and a second token cut to an initial matches one that starts with it. The
memo after “for” is never read. A round trip is automatic, because nothing is reimbursed and
nothing is spent: the two payments cancel. With several equal payments to and from one person,
nothing can say which payment returned which, and nothing depends on it, since the payments are
interchangeable and a P2P link changes no report. So the nearest pairs are linked first, by date
gap and then IDs, and each link records how many candidates it was chosen from. Round
trips run after the transfer and joint rules and use only what they left, so a payment to one of
the user's own names, one that names a tracked account, and a contribution to a joint bill are
never offered. So is a payment whose stored other side is the owner or the co-owner of the joint
accounts (see [contacts](contacts.md)). A saved contact is otherwise no exception: its payments
round-trip like anyone's.

The owner's names, in the forms banks print (see [contacts](contacts.md)), and the nicknames of
tracked accounts are literal alternatives for recognizing the user's transfers, not hard-coded
identities. A name matches whole words only, with spacing and an optional period between them, so
an initial never matches inside another name.

## Not matched and the other side

A transaction is **expected to have a counterpart** when its description, read as rules read it,
is one half of a pair. For money out: a transfer wording, a card-payment wording, a credit-balance
refund, a Zelle payment to one of the user's own names, or a description that names another
tracked account by its last four digits. For money in: a card-payment credit, a transfer
wording, a Zelle payment from one of the user's own names, or a description that names another
tracked account. A purchase, and a payment to someone else, are not expected to pair. Neither is
money in whose category type is Income, such as a settlement paid by Zelle.

A Zelle payment from someone else is expected to pair only while it has no category or one of
the Excluded or Owed type, such as Transfer or Loan: the user has not yet said what it was, or said it is
money that does not count, so its source is a real question. A spending category already answers
it. A Venmo or PayPal payment in, a cash-out included, is never expected to pair. Its sender
writes what they like, so its text cannot be read for meaning, and its other side is in a Venmo or
PayPal balance, whose statements omsync does not import (see [import](import.md)).

A transaction is **not matched** when it is expected to have a counterpart, links do not cover
all of it, no proposal of the run involves it, and its other side is unknown. The run exposes
the list, so the Reconcile page and the Home attention count cannot disagree. Rejecting a proposal
leaves its transactions not matched. What the user is asked to resolve is the review and manual
proposals plus the not-matched list: automatic proposals are applied without the user, and
explained transactions need nothing.

Every transaction has an **other side**: a tracked account, an untracked account of the owner's, a
contact, a merchant, or nobody known (see the [decision record](../adr/0002-identify-the-other-
side.md)). The question is asked of the transactions expected to have a counterpart, and a link
answers it first. Without a link, the other side may still be known because it lies outside the
ledger: a contact is stored on the transaction, recognized after each import or chosen by the user
(see [contacts](contacts.md#the-other-side)). The expected set does not depend on it, and neither
does a proposal. A known other side explains a gap without linking or reconciling anything.

**Explained** transactions are those that would be not matched, with their other side as the
reason. A stored contact reads "Other side: Sam Friend", or "Your account Brokerage" for an account
of the owner's. A Zelle payment from a person that no saved contact claims reads "Other side:" and
the name as written, and nothing is stored for it; saving the person as a contact stores their
payments at once. Money that reads as the user's own, by name, nickname, or a tracked
account, is never explained by a name: a missing link there is an error. Explained is collapsed on
the page, each reason links to the contact, and each row links to the transaction's detail to change
its other side.

Not matched rows can be selected, and "What is on the other side?" sets all of them at once, as one
batch, to an existing contact, one of the owner's accounts outside the ledger, or a new contact made
from a name (see [contacts](contacts.md#setting-the-other-side)). The automatic step follows, so the
rows that are now explained leave the list.

A recorded partner contribution to a joint card bill is asked like any other payment from a person.
It is explained when the co-owner's names or patterns identify the other side, and otherwise stays
not matched.

## Joint settlement rationale

The bill anchor is a payment credit on a joint credit card. It explains why the user sends
half the bill from personal checking or savings to joint checking. The link pairs that
personal debit and joint-checking credit. The card payment is evidence, not a third side
of the settlement link.

```mermaid
flowchart LR
    Personal[Personal checking or savings] -->|User contribution| Joint[Joint checking]
    Partner[Partner's external account] -->|Partner contribution| Joint
    Joint -->|Separate card payment| Card[Joint credit card]
    Card -. Bill anchor .-> Settlement[Explanation of the user contribution]
```

A contribution fits a bill when it is half the bill to within one cent. When the user's payment
description names the joint card, by the account's name or the last four digits of its number,
the payment selects that card's bills by itself and may be within one dollar of half, which
covers a bill that does not split evenly. The joint inflow must then match the payment to the
cent, so the pair explains what was actually sent. Partner contributions use the same slack.
Without a named card the one-cent fit is kept, and a payment that names one joint card is never
matched to another's bill.

Compatible contributions belong to their nearest fitting bill, so equal bills in different
months do not compete indiscriminately. Confirmation evidence narrows the user's joint
inflow but does not bypass amount or date checks. A missing inflow remains a one-sided
manual proposal rather than a fabricated pair.

The user's own joint inflow is one that repeats the outflow's Zelle confirmation, names a tracked
personal account by its last four digits, or carries one of the owner's names or an account
nickname. Any other fitting inflow to joint checking is a partner contribution, whatever its category.

A **partner contribution** has no visible debit in this ledger. It is never linked or annotated. The
proposal names one unambiguous partner inflow, and with several candidates it shows them without
guessing which is the contribution. They are kept out of every other rule. Approved settlement
evidence preserves this exclusion on reruns; a bill remains an anchor even after its own card-
payment link is approved. For a settled bill, the user's payment is the outflow that the
settlement's link records, not a fresh search, so the partner inflows are judged against what was
approved. Undo removes the settlement's effects, allowing the evidence to be considered again.

## Decisions and reruns

Approval refuses a proposal that is no longer current. Selected proposals cannot reuse the
same transaction. Manual links require opposite signs and an amount fitting both available
remainders. Receipt links are created by receipt application, not the manual-link dialog.

Rejection remembers the transaction set and amount independently of link kind. A pair
rejected as a transfer must not return as a P2P explanation; a genuinely different one-sided
proposal remains a different decision. Rejections are excluded before candidate selection
so they do not manufacture ambiguity or consume a better proposal.

Unlinking a link remembers its pair the same way, in the same batch, so undoing the unlink also
forgets it. A removed or rejected pair is never proposed or applied again, which is what makes
automatic application safe to leave on: the next import cannot recreate a link the user removed.
Each remembered pair also keeps the rule that proposed or made it, the evidence, and whether it
was rejected or unlinked. Removing a receipt link records nothing, since receipts are matched by
their own application. Undoing an automatic batch is not a rejection: its proposals return, and
the next import applies them again unless the user unlinks them individually.

A rule is only as trusted as the user's experience of it. A rule with two unlinked links stops
applying automatically: its automatic proposals move to the review queue, with a note saying why,
and stay there. Rejected proposals do not count, because rejecting says a proposal was wrong
before it did harm, while unlinking says an applied link was. The page tallies, for each rule,
the links in place, the links unlinked, and the proposals rejected.

Approval (including the automatic batch), rejection, manual linking, and unlinking use
[batches](changes.md), as does identifying the other side. An automatic apply is atomic, stale-
aware, and revalidated as an approval is, and a failure of the automatic step never undoes the
import before it: the proposals stay available on the Reconcile page. Reconciled state is derived
from link coverage. Approving some proposals and rerunning must neither reuse their money nor hide
evidence required to resolve what remains.