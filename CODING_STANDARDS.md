# Coding standards

Review a diff against these rules. Each needs judgement; flag a violation with the file, the
line, and the rule.

`sh scripts/check.sh` already enforces ruff lint and format, typed SQLAlchemy mappings
(`sqlalchemy.Column` is banned), and import layers: core imports no other omsync package, and
domain packages never import `omsync.web` or `omsync.cli`. The pre-commit hook rejects
statement, spreadsheet, archive, and `.env` files outside `tests/fixtures/`. The commit-msg
hook requires `type(scope): imperative summary`. Don't re-check these by hand.

## Money

- Amounts are integer cents through `omsync.core.money`. Flag float arithmetic on amounts,
  including dividing cents by 100 before scoring, comparing, or rounding.

## Domain behavior

- Web routes and CLI commands call the same domain function. Flag a rule implemented in a
  route, template, or command instead of the domain package.
- Bulk financial writes go through `omsync.core.batches`. A preview writes nothing. Apply is
  atomic, rejects stale input, and is idempotent. Undo refuses to overwrite later work.
- Matching changes preserve ambiguity handling, money conservation, and correct reruns after a
  partial apply. A stronger exclusion must not hide evidence the next run needs.

## Security

- No master password is stored, logged, echoed in errors, or passed in command-line arguments.
- API credentials live only in the keychain. Vault sessions stay in memory. Secrets go into the
  environment of the child process that needs them, never the application's own environment.
- A change to institution website automation states whether it was validated live at that
  institution. Flag it when it doesn't.

## Documentation

- A behavior change updates the feature's architecture page (map in `docs/README.md`) in the
  same diff. A rule lives on one page; flag a copy of it elsewhere.
- Where code and its architecture page disagree, the diff or its description says which is
  intended. Flag code that silently redefines the design.

## Comments

- Comments explain a local implementation subtlety. Flag comments that restate the domain spec,
  narrate development history, or link to documentation.
- Comment density and naming match the surrounding code.

## Tests

- No new test files or test cases unless the user asked for them. Flag any that appear.
- Existing tests change only when the behavior they cover changes.
- Tests the user asked for drive the CLI, an HTTP route, or a Playwright journey, with synthetic
  data in `tmp_path`. They fake external binaries such as `bw` through `shutil.which`. Related
  cases are parametrized.
