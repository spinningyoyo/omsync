# AGENTS.md

omsync is a local, single-user personal finance tracker that listens on `127.0.0.1` only. The
user's real data lives in `~/omsync-data`, never in this repository.

## Where to look

- [docs/README.md](docs/README.md) maps each package to its architecture page. Architecture
  defines intended behavior; code is the implementation. Use its vocabulary.
- Before changing anything that writes financial data, read
  [ledger](docs/architecture/ledger.md) and [changes](docs/architecture/changes.md).
- Review standards: [CODING_STANDARDS.md](CODING_STANDARDS.md).

## Commands

```sh
uv sync
git config core.hooksPath .githooks   # once per clone: pre-commit and commit-msg checks
sh scripts/check.sh                   # ruff lint, ruff format, import layers
uv run pytest -q
eval "$(sh scripts/dev-data.sh | tail -n 1)"   # disposable data seeded from tests/fixtures
uv run omsync serve                   # sign-in needs the user's Bitwarden account
bash scripts/build-css.sh             # after changing template classes
```

## Rules

- Do not add tests unless the user asks for them. Update existing tests when the behavior they
  cover changes.
- Work on disposable data. When the user grants access to their real data, run
  `uv run omsync db snapshot <empty dir>` and point commands at the copy with
  `OMSYNC_DATA_DIR`. Never query or write `~/omsync-data` directly.
- When code contradicts its architecture page, raise it with the user; don't treat the code as
  the design.
- The sign-in scenes are the `omsync-nursery` package, from the sibling `../omsync-nursery`
  repository. omsync follows its `release` branch; `uv.lock` records the commit. Make art changes
  there (it has its own AGENTS.md and the nursery-scene skill) and release them with
  `uv run nursery release` from that checkout. Take a release into omsync with
  `sh scripts/upgrade-nursery.sh`. omsync requires a nursery version range (`>=0.1.1,<0.2`); a release outside it is a contract change, which needs host code and a wider range in the same commit. Never edit the nursery source or the lock entry by hand.
- Changes to institution website automation need live validation at that institution. Say so
  when you can't run it.
