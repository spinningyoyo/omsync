# Contributing to omsync

This guide saves time for you and for the maintainer. omsync is a small project with one
maintainer. Please read this guide before you open an issue or a pull request.

## Philosophy

omsync handles money. A wrong number costs more than a missing feature. Code and architecture
pages stay short, and each page records the decisions that the code cannot show. Start with
[docs/README.md](docs/README.md).

## Contribution gate

Issues are welcome. Pull requests are by invitation only.

1. Open an issue.
2. Wait until the maintainer says yes.
3. Then open a pull request.

The maintainer closes a pull request that has no invitation. The maintainer closes spam without a
reply.

## The one rule

You must understand every line that you submit, and you must be able to explain it. This holds
for code that a tool wrote for you too. The maintainer may ask you to explain a change, and closes
the pull request if you cannot.

## Quality bar for issues

Keep an issue short, concrete and worth reading.

- Write it on one screen.
- Write it in your own voice. Do not use an LLM to write the text.
- State the bug or the idea.
- Explain why it matters.
- Say if you want to build it.

For a bug, give the steps to repeat it, and use the demo (`uv run omsync serve --demo`) when you
can.

## Never include real financial data

Never put real financial data in an issue, a pull request, a test or a screenshot. This includes
statements, account numbers, balances, names and bank logins. Use the demo or synthetic data.
Only `tests/fixtures/` may hold statement files, and they must be made up. The pre-commit hook
rejects statement, spreadsheet, archive and `.env` files elsewhere.

## Before you open a pull request

Run these commands. Both must pass.

```sh
sh scripts/check.sh
uv run pytest -q
```

The browser tests need `uv run playwright install chromium` once. Then follow these rules:

- Do not edit `CHANGELOG.md`. The maintainer writes it.
- Write each commit message as `type(scope): imperative summary`, for example
  `fix(import): keep two equal purchases apart`. The commit-msg hook checks it. Run
  `git config core.hooksPath .githooks` once to turn the hook on.
- Write no unit tests. The tests are end-to-end and live in `tests/e2e/`: a test drives the CLI, an HTTP route or a
  Playwright journey from the outside. Change a test only when your change alters the behavior
  that it covers.
- Update the architecture page of the feature in the same pull request when behavior changes.

## Money rules

Read the Review section of [AGENTS.md](AGENTS.md) before you write code that touches money.

## Bank website automation

A change to the website automation of a bank needs live validation at that bank. The maintainer
checks it with a real account. State in your pull request whether you ran it live. If you cannot,
say so.

## Licence

Your contribution is licensed under the [AGPL-3.0-only](LICENSE), the same as the project.
