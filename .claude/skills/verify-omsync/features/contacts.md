# Contacts

Who money moves with. The list shows each contact's kind and balance. A contact's page has its
balance and open items, and the names, accounts and patterns that recognize it. Your own contact
holds your names, so transfers between your accounts are recognized.

## Sub-features

- list: `/settings/contacts`: Name, Kind, Balance, Open items; "Add contact" dialog.
- owner-card: until you are named, "Add your name …" with "Add your name".
- contact-page: `/settings/contacts/<id>`: header "Delete", "Merge", "Edit"; Balance; Names,
  Accounts, Patterns sections.
- names: "Add name" dialog (Given, Middle, Family); a row's "Delete" icon.
- accounts: "Add account" dialog (Account name, Last four digits); a row's "Add pattern" icon and
  "Delete" icon; its patterns under it.
- patterns: "Add pattern" dialog (Side, Pattern, Names an account); a row's "Delete".
- edit: "Edit" dialog: Name, Kind, and Role with "Set role".
- merge: "Merge" dialog: "Merge into" → "Merge".
- delete: "Delete" dialog → "Delete"; the counterparty on its transactions is cleared.

## How to get to it (user POV)

- Settings → Contacts. A transaction's counterparty links to its contact.

## Driving it with verify.py

Preconditions: any mode. The demo has contacts such as Priya Nair, and your own contact.

- Add contact: "Add contact" → Name, Kind → "Add contact"; the list shows it.
- Names: open it → "Add name" → Given → "Add name"; the row shows "Recognized as:"; its "Delete"
  icon removes it; Undo restores it.
- Account and pattern: "Add account" → name → "Add account"; the row's "Add pattern" icon →
  Pattern → "Add pattern"; the pattern shows under the account; reload.
- Edit: "Edit" → change Name → "Save"; the header changes; reload.
- Delete: "Delete" → dialog → "Delete"; the list no longer shows it; Undo from the toast.

## Gotchas

- Your own contact has no Delete or Merge, and no balance.
- A merge moves names, patterns, accounts and transactions; undo it within the run.
