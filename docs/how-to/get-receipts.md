# Get receipts

Import the statements of the cards first. Then add a store, download its receipts, and apply them.
See [receipts](../architecture/receipts.md) for the design. Commands use `uv run` from the
checkout. Omit it for an installed application.

## Add a store

Open **Settings → Data sync → Receipts** and select **Add store**. Choose Amazon or Costco, and
give the store login a name. With a vault, you can choose its login for Amazon. You can add more
than one login for a store, such as one for each person.

## Download receipts

In the app, select **Sync** on the store's card, or **Sync all**. Sync all syncs the banks first,
then each store. In the terminal:

```sh
uv run omsync sync --all
uv run omsync receipts fetch amazon
uv run omsync receipts fetch costco --start 2026-01-01 --end 2026-03-31
uv run omsync receipts fetch amazon --profile personal
```

Give `--start` and `--end` together. Without them, the download starts 14 days before the newest
receipt of that login. A login with no receipts starts 90 days ago, or 14 days before the first
transaction when that is earlier. Give `--profile` when a store has more than one login.

omsync opens Chrome with its browser profile. Sign in to the store in that Chrome window and
complete any verification. Then select **I have signed in**, or press Enter in the terminal. omsync
saves one file for each receipt:

```text
receipts/AMAZON/<login>/<year>/<order ID>.json
receipts/COSTCO/<login>/<year>/<barcode>.json
```

If the download stops, the finished receipts stay on disk. Run the same range again to fetch the
rest. If Costco rejects the session, sign in again and retry. Do not share the JSON files casually.
They can contain membership identifiers.

omsync does not read the Amazon data export from the "Request my data" page. See
[ADR 0003](../adr/0003-read-amazon-orders-from-the-payment-history.md). See
[vendor requests](../architecture/vendor-requests/README.md) for the request details.

## Preview and apply receipts

Add receipt item keywords in **Categories → Keywords**. Choose the store's list. For Costco, you can
also choose a category for food items that have no keyword. Or preview and import a vendor keyword
file:

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
After you import keywords, run the apply again. omsync does not duplicate charges that it already
applied. To reverse the operation, run `uv run omsync batches undo BATCH_ID`. Later work can block
the undo.
