# AGENTS.md

omsync is a local, single-user personal finance tracker that listens on `127.0.0.1` only. The
owner's real data lives in `~/omsync-data`, never in this repository. Start with
[docs/README.md](docs/README.md). It maps each package to its architecture page. Code is the
behavior. Architecture pages record the key decisions, with diagrams. Use their vocabulary. Keep
them high level, and do not restate code. Write them in ASD-STE100 style with the same five
sections.

## Rules

- Before you change anything that writes financial data, read
  [ledger](docs/architecture/ledger.md) and [changes](docs/architecture/changes.md).
- Work on disposable data. When the owner grants access to the real data, run
  `uv run omsync db snapshot <empty dir>` and point commands at the copy with `OMSYNC_DATA_DIR`.
  Never query or write `~/omsync-data` directly.
- When code contradicts its architecture page, raise it with the owner. Do not treat the code as
  the design.
- During a feature or a bug fix, do not change the documentation: `docs/`, the in-app guides,
  `README.md` and `CHANGELOG.md`. When the code work is complete, ask the owner if you can update
  the documentation.
- Tests are end-to-end only, in `tests/e2e/`. Do not write unit tests. A test drives the CLI, an HTTP route or a
  Playwright journey from the outside. Do not add tests unless the owner asks. Update an existing
  test when the behavior that it covers changes.
- The sign-in scenes and the garden around them (scene picker, watering, saved growth, day and
  night) are the `omsync-nursery` package, from the sibling `../omsync-nursery` repository. Put
  behavior that the garden needs in any app into the nursery, not into omsync. omsync follows its
  `release` branch, and `uv.lock` records the commit. Make art and garden changes there (it has
  its own AGENTS.md and the nursery-scene skill). Release them with
  `uv run nursery release` from that checkout. Take a release into omsync with
  `sh scripts/upgrade-nursery.sh`. The version range is in `pyproject.toml`. A release outside it
  is a contract change: change the host code and the range in one commit. Never edit the nursery
  source or the lock entry by hand.
- The mascot and the favicons in `src/omsync/web/static/brand/` come from `ink-studio`. `ink.toml`
  lists them, and `ink.sha256` records their content. Never edit them here. Change the art in
  ink-studio and export it. `sh scripts/check.sh` fails when a file differs from `ink.sha256`.
- A change to the website automation of an institution needs live validation at that institution.
  Say so when you cannot run it.
- The `verify-omsync` skill drives the real app in a browser on throwaway data. Ask the owner
  before you run it, and say which features you would drive.

## Review

`sh scripts/check.sh` and the git hooks enforce lint, format, import layers, and commit messages.
Check these rules by hand:

- Amounts are integer cents through `omsync.core.money`. Do not use floats, also not to divide
  cents by 100 before a comparison or a rounding.
- A rule lives in the domain package. Web routes and CLI commands call the same domain function.
- Bulk financial writes go through `omsync.core.batches`.
- A matching change keeps ambiguity handling, money conservation, and correct reruns after a
  partial apply.
- A master password is not stored, logged, shown in an error, or passed as an argument. A secret
  goes only into the environment of the child process that needs it.
- A comment explains a local detail of the code. It does not restate the design, tell history, or
  link to docs.
- Tests use synthetic data in `tmp_path`. They fake external binaries such as `bw` through
  `shutil.which`. Related cases are parametrized.
- A behavior change updates its architecture page when the owner approves the documentation
  work.

## Commands

```sh
uv sync
git config core.hooksPath .githooks   # once per clone: pre-commit and commit-msg checks
sh scripts/check.sh                   # ruff, Biome (JS and CSS), import layers
uv run playwright install chromium    # once: the browser tests need it
uv run pytest -q
uv run pytest -m browser              # browser journeys, about 90 seconds: run after a page change
eval "$(sh scripts/dev-data.sh | tail -n 1)"   # disposable data seeded from tests/fixtures
uv run omsync serve --demo            # needs no credentials
uv run omsync serve                   # sign-in needs the owner's Bitwarden account
bash scripts/build-css.sh             # after you change template classes
```

## Git

- Commit only when the owner asks.
- Stage explicit paths: `git add <path1> <path2>`. Never run `git add -A` or `git add .` in a
  shared tree. Run `git status` before you commit, and stage only your files.
- Never run commands that destroy work or skip checks: `git reset --hard`, `git checkout .`,
  `git clean -fd`, `git stash`, `git commit --no-verify`.
- Never force-push. If a rebase conflict is in a file that you did not change, stop and ask.
- Write each commit message as `type(scope): imperative summary`. The commit-msg hook enforces it.

## Changelog

- `CHANGELOG.md` follows Keep a Changelog. Put each new entry under `## [Unreleased]`, in
  `### Added`, `### Changed`, `### Fixed` or `### Removed`. Read the section first. Add to its
  subsections instead of repeating them.
- Never change a released section.
- Write for the person who runs omsync. Do not list linters, checks, tests or CI.
- Contributors do not edit the changelog.
