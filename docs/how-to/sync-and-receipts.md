# Run sync and obtain receipts

A sync fetches statement files and imports them. A receipt download writes files even when no
ledger changes are requested. Statement import and receipt matching have
separate read-only previews. See [sync](../architecture/sync.md) and
[receipts](../architecture/receipts.md) for their design contracts.

Commands use `uv run` from the checkout; omit it for an installed application. Substitute
your configured data directory for `~/omsync-data` in the examples.

## Configure and run institution sync

Run `uv run omsync doctor` to identify missing setup. In Settings, open Bank sync and add a
provider for the bank: choose its connector, select its Bitwarden login, and choose the accounts
it fetches. Confirm the vault item's first URI is the institution's genuine sign-in address: this
is where the connector types its credentials. Set `email` in `omsync.toml`, or pass `--email`
below.

Close Chrome windows already using omsync's profile. The connector starts or attaches to
Chrome with that dedicated profile. Sync from the Bank sync page with Sync all or a provider's
Sync button, or run sync in the terminal. Either one asks for the master password, and any
institution verification code is asked on the page or in the terminal. Replace `example` with
the provider key:

```sh
uv run omsync sync example --email you@example.com
uv run omsync sync --all --email you@example.com
```

Each sync imports the files it fetched, one batch per file. `--all` runs every enabled provider
in turn; a provider that fails does not stop the others, and the command exits 1 if any failed.
To undo an import, find its batch with `uv run omsync batches list` and run
`uv run omsync batches undo BATCH_ID`.

To sync a period, and to review the log and the providers:

```sh
uv run omsync sync example --from 2026-01-01 --to 2026-03-31
uv run omsync sync-log --limit 20
uv run omsync providers list
```

Give `--from` and `--to` together. Without them, sync fetches from 45 days before the latest
transaction in that provider's accounts, or January 1 when there is no activity, through today.
The institution may return statement-based periods rather than exactly that range.

If another sync, receipt download, or backup holds the operation lock, wait for it to finish
and retry. For verification or session problems, complete the institution's requested
interaction and rerun. For an account not shown, check configured labels and aliases. When
a card was reissued, open the account's page, add the new card's last four under Numbers, then
sync. From the terminal:

```sh
uv run omsync accounts list
uv run omsync accounts alias --account-id ACCOUNT_ID --institution chase --statement-account 5678
```
For a changed website, retain files obtained directly from the institution and use the
[import guide](import-statements.md), rather than guessing at browser automation changes.

## Obtain Amazon receipts

Use Amazon's Request my data page to request orders and returns/refunds. Unzip each vendor
login's files into its own directory, preserving Amazon's inner folder names:

```text
receipts/AMAZON/<vendor login>/Your Amazon Orders/Order History.csv
receipts/AMAZON/<vendor login>/Your Returns & Refunds/Refund Details.csv
```

The login label is your folder name. Keep order and refund files for the same login together,
since refund payer and item evidence comes from that login's order history. These are
private files, not repository fixtures.

## Obtain Costco receipts

Start Chrome with omsync's profile and loopback remote debugging. With the default data
location on a Mac:

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
  --user-data-dir="$HOME/omsync-data/browser-profile"
```

Then download a purchase-date range:

```sh
uv run omsync receipts fetch costco --start 2026-01-01 --end 2026-03-31
```

Both dates are required and the end cannot be in the future. Sign in to Costco in that
Chrome window, complete any verification, and press Enter in the terminal when prompted.
This uses the browser session, not the vault. Receipts are saved as:

```text
receipts/COSTCO/<year>/<quarter>/<barcode>.json
```

Session rejection means sign in again and retry. If interrupted, completed receipts remain
on disk; rerun the same range to fetch the rest. Do not share JSON files casually: they can
contain membership identifiers.

## Preview and apply receipts

Import the supporting transactions first. Add receipt-item keywords in Settings → Categories, or
preview/import a vendor keyword file:

```sh
uv run omsync keywords import /path/to/keywords.json --list amazon
uv run omsync keywords import /path/to/keywords.json --list amazon --apply
uv run omsync receipts vendors
uv run omsync receipts run amazon
uv run omsync receipts run costco
```

Read the outcome counts and review reasons. Missing item categories need triage. Unknown
payer, changed receipt facts, or ambiguous transaction evidence requires review, not a
forced automatic match. Categories and splits you assigned yourself remain protected.

Apply the intended vendor, optionally limiting application by transaction posted date:

```sh
uv run omsync receipts run amazon --start 2026-01-01 --end 2026-03-31 --apply
uv run omsync receipts run costco --apply
```

`--start` and `--end` must be supplied together. Without them, application covers all history.
The selected period limits application, not the history used to assess ambiguity. Reconcile
also offers receipt previews and application for its selected range.

Apply reports the batch ID for undo. A partial apply can be completed after item triage by
rerunning; already applied charges are not duplicated. Use `uv run omsync batches undo
BATCH_ID` if the operation needs reversal and no later work blocks it.
