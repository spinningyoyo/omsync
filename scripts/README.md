# Scripts

These scripts support development, the sign-in scenes, bank automation and ledger maintenance; run each command from the repository root.

## Everyday development

### check.sh

- Command: `sh scripts/check.sh`
- What it does: Runs the static checks. These are ruff lint, ruff format check, Biome for the JavaScript and CSS, and the import-layer check. It runs every check and shows all the failures. It does not run the tests.
- Writes: Nothing. The tools only read the code. They can keep caches in ignored folders.

### biome.sh

- Command: `sh scripts/biome.sh check .` (add `--write` to fix what it can)
- What it does: Runs Biome, the linter and formatter for the JavaScript and CSS under `src/omsync/web`. It uses the pinned Biome binary. The first run downloads that binary, checks its SHA-256 digest and stores it in `.tools/`. `scripts/check.sh` runs it.
- Writes: With `--write`, the JavaScript and CSS source files. The first run also writes `.tools/`, which is ignored.

### build-css.sh

- Command: `bash scripts/build-css.sh`
- What it does: Builds `src/omsync/web/static/css/app.css` from the Tailwind source. It uses the pinned Tailwind CLI. The first run downloads that CLI, checks its SHA-256 digest and stores it in `.tools/`.
- Writes: The repository file `src/omsync/web/static/css/app.css`, which is committed. The first run also writes `.tools/`, which is ignored.

### dev-data.sh

- Command: `sh scripts/dev-data.sh [DIR]`
- What it does: Builds a disposable database and statement folders from the synthetic statements in `tests/fixtures`. It prints an `export` line that points omsync at the new folder. To use it, run `eval "$(sh scripts/dev-data.sh | tail -n 1)"`.
- Writes: A disposable directory. It uses `DIR` when you give one. Otherwise it creates a new `omsync-dev.*` folder in `$TMPDIR`, or in `/tmp`. The script stops if `DIR` already has `omsync.db`. **It does not write your ledger.**

### guide-screenshots.py

- Command: `uv run python scripts/guide-screenshots.py`
- What it does: Takes the screenshots that the in-app Guides show, in light and dark themes. It starts `omsync serve --demo` and drives headless Google Chrome, which must be installed. When it ends, it stops the server and deletes its temporary folder.
- Writes: Repository files. It writes PNG pairs to `src/omsync/web/static/img/guides/`, which are committed. The demo uses its own temporary data. **It does not write your ledger.**

## Nursery (sign-in scenes)

### dev-nursery.sh

- Command: `sh scripts/dev-nursery.sh serve`. Any other omsync command also works after the script name.
- What it does: Runs omsync on the live nursery checkout, at `../omsync-nursery` or at the path in `NURSERY`. It does not use the release that `uv.lock` records. Art changes show before a release.
- Writes: **Writes your ledger.** When a ledger exists in your data directory, `serve` upgrades it to the head revision and runs the automatic step once. It also holds `serve.lock` in that directory. The app writes to the ledger while it runs. Without a ledger, `serve` runs the demo and writes nothing to your data directory. To keep your ledger out of it, set `OMSYNC_DATA_DIR` to a disposable folder first. See `dev-data.sh`.

### upgrade-nursery.sh

- Command: `sh scripts/upgrade-nursery.sh`
- What it does: Moves omsync to the newest release on the nursery `release` branch. It re-locks the package, runs the checks and the tests, and commits `uv.lock` when they pass. It stops when the release is outside the version range in `pyproject.toml`. On a failure, it restores the old `uv.lock`. It needs a clean `pyproject.toml` and `uv.lock`.
- Writes: Repository files only. It changes `uv.lock`, makes a git commit, and updates the `.venv` folder. **It does not write your ledger.**

## Bank automation tools

### bank_record.py

- Command: `uv run python scripts/bank_record.py https://<bank address> [capture-folder]`
- What it does: Opens Google Chrome on a temporary profile, with remote debugging on port 9333. You sign in and download a statement yourself. The script records the requests and responses. It saves cookies and browser storage when you press Enter, or when you touch `control/snap` or `control/stop` in the capture folder. It does not save sign-in request bodies or fields named like a password.
- Writes: A capture folder. By default the script creates a new temporary folder. A folder you give must be outside the repository and outside your data directory, or the script stops. A capture holds live session cookies and statement files, so delete it after use. **It does not write your ledger.**

### bank_trace.py

- Command: `uv run python scripts/bank_trace.py <capture-folder> [regex]`
- What it does: Reads a capture from `bank_record.py`. With no regex, it lists download-like requests and stored token names. With a regex, it traces where each request value first appeared. The places are a response body, a response header, an earlier request, a cookie or browser storage. It prints names, lengths and places. It does not print values.
- Writes: Nothing. It prints to the terminal and writes no files.

## Ledger maintenance

### merge_institution.py

- Command: `uv run python scripts/merge_institution.py SOURCE_KEY TARGET_KEY` for a dry run. Add `--apply` to merge.
- What it does: Moves the accounts, aliases and providers of one institution to another. Then it deletes the source institution. Transactions and sync history follow their accounts. The statement folder of the source is not moved. The script stops, and writes nothing, when a statement account ID clashes at the target. It also stops when an open account of the source has the same last four digits as an open account at the target.
- Writes: Without `--apply`, nothing. With `--apply`, **Writes your ledger**. It writes one batch that you can undo with `uv run omsync batches undo <batch id>`. The ledger must be at the head revision.

## One-off migration from maneko

No script in this folder migrates data from maneko. `.gitignore` names `scripts/maneko_*.py` and `scripts/setup_institutions.py` as local one-off scripts. They are never committed.
