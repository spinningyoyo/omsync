# Backlog

Problems worth solving later, written as problem statements. Remove an entry when it becomes a
feature, and put the resulting behavior in the matching architecture page.

## An account's joint status changes over time

**Problem.** An account has one joint flag. A real account can be joint for a while and solo
before and after, such as a card that was shared for a short period. With a single flag, either
choice misstates history. Marking the account joint today treats its solo months as shared.
Marking it solo drops the shared months from joint settlement and the joint share.

**Who it affects.** Anyone whose joint settlement or joint reports span a change in who shares
an account.

**What a solution must do.**

- Record joint status with the dates it applied, so a transaction takes the status that was in
  effect on its date.
- Make a change of joint status a previewed, undoable batch, like other bulk financial changes.
- Keep settlement and reports consistent: a period already closed or settled must not change
  silently when the status history is edited.

**Open questions.**

- How is a change entered: an effective date on the account, or a date range per status?
- What happens to an existing settlement that covers a transaction whose status changes?
- Does a joint status ever differ between two people's views of the same account?
