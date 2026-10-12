# Import and review statements

Use this guide for statement files obtained directly from an institution or already saved
by sync. [Import architecture](../architecture/import.md) explains identity decisions;
[changes](../architecture/changes.md) explains preview, apply, and undo.

Commands below use the checkout through `uv run`. An installed application uses the same
commands without that prefix. Paths assume the default data directory; substitute yours
when it is configured differently.

## Prepare accounts and files

Create or upgrade the database before terminal import:

```sh
uv run omsync db upgrade
uv run omsync accounts list
```

In the app, Settings → Bank sync → Add provider sets up a provider, its accounts, and the mapping of its statement accounts.
For terminal setup, supply the account ID reported in the file, not just its last-four label:

```sh
uv run omsync accounts add --institution example --institution-name "Example Institution" \
  --statement-account STATEMENT_ACCOUNT_ID --name Checking --type checking
```

Replace `STATEMENT_ACCOUNT_ID` with the institution's actual value. Treat that value as
private, including in shell history. Use the app's mapping flow if you do not want to put
it in terminal arguments. Add `--joint` for a shared account. A reissued card can map its
new statement ID to an existing account using Settings rather than creating another account.

Place files under the configured data directory:

```text
statements/EXAMPLE/activity.ofx       # OFX/QFX contains its account ID
statements/BOA/1234/activity.csv      # BoA CSV uses the account folder as its ID
```

The institution folder corresponds to its configured key, ignoring case. BoA CSV needs an
account ID supplied by the folder or `--account`; it does not contain one itself.

## Preview, then apply

```sh
uv run omsync import
```

Read each file's joined, created, and review counts. An unmapped account reports a masked
label and a setup hint. Map it and preview again. An invalid file reports the file and
problem without printing its contents.

To preview a file outside the statement folder:

```sh
uv run omsync import /path/to/activity.ofx --institution example
uv run omsync import /path/to/activity.csv --institution boa --account 1234
```

When satisfied, repeat the same command with `--apply`, or apply the pending folder files:

```sh
uv run omsync import --apply
```

Preview writes nothing. Apply retains outside files in the statement folder and creates
one batch per file. Valid files may import even when another file fails, so read the output
rather than assuming a nonzero exit means that nothing happened. Repeating a completed
import reports no pending work or “already imported.”

## Resolve review and categorize

Open Transactions → Needs review. Inspect the source row and its possible existing
transactions. Choose Same as when it is the same movement, or Add as new transaction when
it is genuinely distinct. Do not create another transaction simply to empty the queue.

Check account balance issues after import. Open the interval's transactions and compare
source evidence before deciding that an apparent duplicate or missing movement is real.
“No statement balances” means there is no balance evidence to compare, not a proven balance.

Open Categories to triage payees and preview categorization. The terminal equivalent is:

```sh
uv run omsync categorize
uv run omsync categorize --apply
```

## Undo an unwanted import

```sh
uv run omsync batches list
uv run omsync batches undo BATCH_ID
```

Replace `BATCH_ID` with the import's batch. Undo removes its database effects, not the
statement file. A later edit or dependent import can block undo; read the conflict and
undo dependent work first if that is the result you intend. Do not bypass the refusal
with direct database edits.
