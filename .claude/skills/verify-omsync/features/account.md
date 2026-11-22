# Account

An account's own page: its balance chart, statements, the numbers that identify it (statement
aliases and receipts-only numbers) and its nicknames for payment apps.

## Sub-features

- account-page: name, type, balance, the chart of statement balances, View transactions.
- edit-account: "Edit account": name, joint, closed, displayed last four.
- numbers: the list of numbers; "Add number" with "The bank does not show it" for receipts-only.
- nicknames: the list; "Add nickname"; a row's Delete icon.

## How to get to it (user POV)

- Click an account in the accounts panel, or `/accounts/<id>`.

## Driving it with verify.py

Preconditions: any mode. Find an id from the accounts panel link.

- Open: `page.get_by_role("complementary", name="Accounts panel").get_by_role("link", name=re.compile("Everyday Checking")).click()`.
- Edit: the "Edit account" control; change the name; Save; reload; the accounts panel shows it.
- Add number: fill "Another card's last four digits" with 4 digits, tick "The bank does not show
  it", "Add number"; the list shows it; Undo removes it.
- Add nickname: fill "Nickname", "Add nickname"; the row appears; its "Delete" icon removes it;
  Undo restores it.

## Gotchas

- A number shorter than four digits is refused by the field's pattern before it posts.
