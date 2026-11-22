# Back up and restore

Use a full archive to recover the database together with the statement and receipt evidence.
[Changes, undo, and backups](../architecture/changes.md) defines what an archive holds, what it
leaves out, and which locks apply. Commands use `uv run` from a checkout. Omit it for an installed
application.

An archive holds readable financial data. Store it on encrypted storage. Keep a copy outside this
Mac. Checksums detect damage, not origin. Restore only archives that you trust.

## Create and list archives

```sh
uv run omsync backup create
uv run omsync backup list
```

Create prints the archive path and the database revision. If a sync or receipt download is
running, create refuses. Retry when it finishes. Back up after financial work and before a risky
change. omsync never prunes full archives.

`uv run omsync db backup` makes a copy of the database only. It helps with database recovery. It
is not a full archive, and `omsync backup restore` cannot read it.

## Verify before restoring

Restore refuses while `omsync serve`, a sync, or a receipt download runs. Stop the server and wait
for the rest to finish. Pick an archive path from `backup list`. Then check the archive without
replacing anything:

```sh
uv run omsync backup restore /path/to/omsync-backup-STAMP.tar.gz
```

For an archive in the configured `backups/` directory, the file name alone works. The command
checks the paths, the file checksums, and the database revision. It describes what a restore
replaces. It does not change your data.

- **The check fails:** use an intact archive that you trust.
- **The database revision is unknown:** use an omsync version that can read it. Then restore.

## Replace current data

```sh
uv run omsync backup restore /path/to/omsync-backup-STAMP.tar.gz --yes
```

This replaces the database, statements, and receipts as a whole. It does not merge later
transactions into the archived state. omsync first saves your current data in a pre-restore
archive. Files that you added after the selected backup stay in that safety archive.

An existing config file stays. If none exists, omsync writes the archived config. Check its paths
before the next launch. The restored database moves to the current schema when needed. After
success, start the app and check your accounts and source files.

If a file replacement fails, omsync puts the previous data back. If the process stops part way,
keep these until you recover:

- any `.restore-*` directory under the data directory;
- the pre-restore archive.

The moved-aside data is in `previous/`. Do not delete it to clear an error. If the later schema
upgrade fails, keep the safety archive and find the cause before you resume financial work.

## Rehearse recovery safely

A restore drill uses a scratch data directory and config. The scratch directory still holds
private data. Use protected storage, and never put it in the repository. Run the drill in a
separate shell, so the temporary environment cannot redirect later commands:

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

Replace the archive path before you run it. Confirm that the accounts and the expected statement
and receipt files are present. The scratch directory stays for inspection. Remove that one
directory when you finish. Never use the real data directory in the cleanup. An empty destination
needs no pre-restore archive, because it holds no financial data to save.

## Restore onto another Mac

1. Install omsync.
2. Restore the archive into the data directory that you want.
3. Keep or create the machine config for that destination. Do not assume the archived paths suit
   the new Mac.
4. Run `uv run omsync doctor`.
5. If you use Bitwarden, enter the API credentials at the secret prompts, then connect the vault on
   the Data sync page:

```sh
uv run keyring set omsync bitwarden-client-id
uv run keyring set omsync bitwarden-client-secret
```

Sign in to the institution and Costco websites again in the dedicated browser profile. The archive does not carry those sessions. A backup never holds your
master password.
