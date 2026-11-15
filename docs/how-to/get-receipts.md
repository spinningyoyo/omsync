# Get receipts

Receipts are downloaded and applied from the terminal. Import the statements of the cards first.
See [receipts](../architecture/receipts.md) for the design. Commands use `uv run` from the
checkout. Omit it for an installed application.

## Obtain Amazon receipts

Use the Request my data page at Amazon. Request orders and returns/refunds. Unzip the files of
each vendor login into their own directory. Keep the inner folder names that Amazon uses:

```text
receipts/AMAZON/<vendor login>/Your Amazon Orders/Order History.csv
receipts/AMAZON/<vendor login>/Your Returns & Refunds/Refund Details.csv
```

The login label is your folder name. Keep the order files and refund files of one login together.
The refund payer and the items come from the order history of that login. These files are private.
Never use them as repository fixtures.

## Obtain Costco receipts

Start Chrome with omsync's profile and loopback remote debugging. With the default data location
on a Mac:

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
  --user-data-dir="$HOME/omsync-data/browser-profile"
```

Then download a range of purchase dates:

```sh
uv run omsync receipts fetch costco --start 2026-01-01 --end 2026-03-31
```

Both dates are required. The end date cannot be in the future. Sign in to Costco in that Chrome
window and complete any verification. Then press Enter in the terminal when it asks. This uses the
browser session, not the vault. omsync saves the receipts as:

```text
receipts/COSTCO/<year>/<quarter>/<barcode>.json
```

If Costco rejects the session, sign in again and retry. If the download stops, the finished
receipts stay on disk. Run the same range again to fetch the rest. Do not share the JSON files
casually. They can contain membership identifiers.

## Preview and apply receipts

Import the supporting transactions first. Add receipt item keywords in Settings → Categories. Or
preview and import a vendor keyword file:

```sh
uv run omsync keywords import /path/to/keywords.json --list amazon
uv run omsync keywords import /path/to/keywords.json --list amazon --apply
uv run omsync receipts vendors
uv run omsync receipts run amazon
uv run omsync receipts run costco
```

Read the outcome counts and the review reasons. Triage the items that have no category. An unknown
payer, a changed receipt, or an unclear transaction goes to review. omsync never forces an
automatic match. Categories and splits that you set stay protected.

Apply the vendor that you want. To limit the apply by the posted date of the transactions, add a
period:

```sh
uv run omsync receipts run amazon --start 2026-01-01 --end 2026-03-31 --apply
uv run omsync receipts run costco --apply
```

Give `--start` and `--end` together. Without them, the apply covers all history. The period limits
what omsync applies. It does not limit the history that omsync uses to judge an unclear match.
Reconcile also previews and applies receipts for its selected range.

Apply prints the batch ID for undo. A keyword that you add in Triage applies the receipts at once.
After you add keywords another way, run the apply again to finish a partial apply. omsync does not duplicate charges that it already applied. To reverse the operation, run
`uv run omsync batches undo BATCH_ID`. Later work can block the undo.
