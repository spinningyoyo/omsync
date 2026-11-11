# Back up and restore

Use a full archive to recover the database together with statement and receipt evidence.
[Changes, undo, and backups](../architecture/changes.md) defines what is included and how
replacement is protected. Commands use `uv run` from a checkout; omit it for an installed
application.

Archives contain readable financial data. Store them on encrypted storage and keep a copy
outside this Mac. Checksums detect damage, not provenance: restore only archives you trust.

## Create and list archives

```sh
uv run omsync backup create
uv run omsync backup list
```

Create prints the archive path and database revision. It includes the database, statements,
receipts, and active config file, not keychain entries, vault state, browser sessions,
exports, or existing backups. A concurrent sync or Costco download causes refusal; retry
when it finishes. Back up after financial work and before a risky change. Full archives are
not automatically pruned.

`uv run omsync db backup` makes a database-only copy. It is useful for database recovery,
but it is not a full archive and cannot be read by `omsync backup restore`.

To inspect or experiment on your data without touching it, copy the database into a new data
directory. The copy is migrated to the current schema; the original is left as it was:

```sh
uv run omsync db snapshot /path/to/empty-dir
OMSYNC_DATA_DIR=/path/to/empty-dir uv run omsync categorize
```

## Verify before restoring

Stop `omsync serve` and wait for any sync or receipt download to finish. Select an archive
path from `backup list`, then check it without replacement:

```sh
uv run omsync backup restore /path/to/omsync-backup-STAMP.tar.gz
```

A file name alone also works for an archive in the configured `backups/` directory. The
command verifies paths, file checksums, and database revision, and describes replacement.
It does not replace current financial data. If verification fails, use an intact trusted
archive. If the database revision is unrecognized, use an omsync installation that can
read it before attempting restore.

## Replace current data

```sh
uv run omsync backup restore /path/to/omsync-backup-STAMP.tar.gz --yes
```

This replaces the database, statements, and receipts as a whole. It does not merge later
transactions into the archived state. Existing data is saved first in a pre-restore archive;
files added after the selected backup are recoverable from that safety archive.

An existing config file is kept. If none exists, the archived config can be written; check
its paths before the next launch. The restored database is upgraded to the application's
schema when needed. After success, start the app and inspect accounts and source files.

A file-replacement failure rolls the previous data back. If the process was interrupted,
keep any `.restore-*` directory under the data directory and the pre-restore archive until
you have recovered. The moved-aside data is under `previous/`; do not delete it to dismiss
an error. If a subsequent schema upgrade fails, preserve the safety archive and diagnose
the failure before resuming financial work.

## Rehearse recovery safely

A restore drill uses a scratch data directory and config. It still contains private data,
so use protected storage and never put it in the repository. Do this in a separate shell
so the temporary environment cannot redirect later normal commands:

```sh
(
  SCRATCH="$(mktemp -d)"
  export OMSYNC_DATA_DIR="$SCRATCH/data" OMSYNC_CONFIG="$SCRATCH/omsync.toml"
  uv run omsync backup restore /absolute/path/to/omsync-backup-STAMP.tar.gz --yes
  uv run omsync accounts list
  find "$OMSYNC_DATA_DIR/statements" "$OMSYNC_DATA_DIR/receipts" -type f
  printf 'Scratch data remains at %s\n' "$SCRATCH"
)
```

Replace the archive path before running. Confirm accounts and expected statement/receipt
files are present. The displayed scratch directory remains for inspection; remove that
specific directory when finished. Never substitute the real data directory in the cleanup.
An empty destination needs no pre-restore archive because it has no financial state to save.

## Restore onto another Mac

Install omsync and restore the archive into the intended data directory. Keep or create
machine config with that destination rather than assuming archived paths suit the new Mac.
Run `uv run omsync doctor` and restore API credentials through secret prompts:

```sh
uv run keyring set omsync bitwarden-client-id
uv run keyring set omsync bitwarden-client-secret
```

Unlock the vault when requested, and sign in to institution and Costco websites again in
the dedicated browser profile. Those sessions are deliberately not transported in the
financial archive. Your master password is never part of a backup.
