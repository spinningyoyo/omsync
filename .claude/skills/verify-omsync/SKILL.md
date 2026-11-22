---
name: verify-omsync
description: Drive the real omsync web app in headless Chrome on throwaway data, the way the owner uses it, and keep screenshots and page snapshots as proof. Use when the owner asks to verify, check or sweep the app, or after a page change once the owner agrees to a run. Ask the owner first and name the features you would drive. Not for the owner's live data.
---

# Verify omsync

The tests in `tests/e2e/` check HTML that routes return. They do not click, and they do not see
what is on the screen, so a dialog behind a modal, a menu cut off by its list, or a label that is
out of date passes them. This skill launches the real server, signs in, uses the pages with
Playwright, and checks what a person would see, then reloads to check that it stayed.

Ask the owner before a run (AGENTS.md), and name the features you would drive. Run every command
from the repository root.

## Pick a flow

The fixed checks come first. They are the browser journeys in `tests/e2e/test_browser_*.py`, one
or more per feature file, and they run the same steps every time:

```sh
uv run pytest -m browser                                   # every journey, about 90 seconds
uv run pytest -m browser tests/e2e/test_browser_sync.py   # one area
```

Use this skill's own launch and drive only for what the journeys do not cover yet:

- **A new or changed page** before its journey exists. Drive it once here, then write the journey
  in `tests/e2e/test_browser_*.py` with `browser_support.py`, so the next check is fixed.
- **A visual look** at screenshots, such as a dark theme or a layout, that no assertion covers.
- **A sweep the owner asks for:** walk [`features/README.md`](features/README.md) top to bottom.

## Launch

```sh
uv run python .claude/skills/verify-omsync/scripts/verify.py launch            # ledger mode
uv run python .claude/skills/verify-omsync/scripts/verify.py launch --mode demo
```

It prints the run directory, `RUN` below, once `/health` answers. Building the data takes about a
minute. `RUN/run.json` has the port, the base URL and the process group. The server log is
`RUN/serve.log`.

- **ledger mode** (the default) serves the demo's made-up data as an ordinary ledger. Every page
  behaves as it does for the owner, including Sign in, the vault and the Data sync page. Two outside
  systems are stand-ins: the owner check always passes, and Bitwarden is an in-memory vault. Its
  master password is `verify-master`, and it has four logins: `Chase (verify)`, `Amazon (verify)`,
  `Costco (verify)` and `Bank of America (verify)`. **Sync is off:** a sync signs in to real banks
  and stores, so Sync and Sync all answer "Sync is off in verification runs".
- **demo mode** runs `omsync serve --demo`. Use it only for the demo's own sign-in ("Explore the
  demo"), the Demo pill and Reset demo. Its vault refuses everything.

The server runs the code from launch time and does not reload. After an edit, clean up and launch
again.

## Doctor

```sh
uv run python .claude/skills/verify-omsync/scripts/verify.py doctor RUN
```

Read-only. It checks that the process is alive, `/health` answers, the port belongs to this run, the
data directory is this run's, and `src/` has not changed since launch. Run it first whenever
anything looks off. Drive nothing that fails it.

## Drive

Write a Python file of Playwright steps, then run it:

```sh
uv run python .claude/skills/verify-omsync/scripts/verify.py drive RUN path/to/drive.py [--theme dark]
```

The helper starts headless Chromium at 1280×900, sets the theme, turns privacy blur off, signs in,
and runs the file with these globals:

- `page`: the signed-in Playwright page.
- `base`: the server's URL, such as `http://127.0.0.1:63401`.
- `shot(name)`: waits for htmx to settle, then writes `RUN/evidence/<name>.png` (full page) and
  `RUN/evidence/<name>.aria.txt` (the accessibility tree).
- `settle()`: waits until the network is idle and no htmx request is in flight.
- `MASTER_PASSWORD`: the stand-in vault's master password.

An uncaught error writes `evidence/failure.png` and exits non-zero. Browser console errors go to
`evidence/browser-errors.txt`; a deliberate refusal, such as a wrong password, shows there as a 422.

Conventions:

- Find controls by role and accessible name, the words on the screen: `get_by_role("button",
  name="Add tag")`, `get_by_label("Password", exact=True)`, `get_by_role("dialog", name="Edit
  Amazon")`. Never use coordinates or tab order.
- Assert with `from playwright.sync_api import expect`. It waits for the state; never `sleep`.
- A card or row also contains its closed dialogs, so a text search in it can match twice. Scope to
  the part you mean, such as the card's `dl`, or a heading with `exact=True`.
- Give anything you create a unique name (add the time), so a second drive in the same run does
  not collide with the first.
- A journey in `tests/e2e/test_browser_*.py` is the best starting point: copy its steps into a
  drive script, with `page` and `base` in place of the test's `user` and `live.url`.

## Evidence

A journey is proven only when all of these hold:

1. It goes the way the owner goes: the rail, the page, the button, the dialog. Never a direct POST
   or a route the page does not use.
2. It checks the result on screen after the action, then again after `page.reload()`, so a write
   that did not stick fails.
3. Undo is part of the journey wherever a toast offers it: click Undo, reload, and check that the
   old state is back.
4. It has a screenshot of the state before and after the action.
5. A path that this setup cannot reach, such as a real sync, is named in the report with the reason.
   It is never reported as verified through another path.

Report each journey as PASS or FAIL with its evidence paths, and list what was not reached.

## Cleanup

```sh
uv run python .claude/skills/verify-omsync/scripts/verify.py cleanup RUN
```

It stops this run's process group and deletes `RUN/data`. `RUN/evidence` stays. Never stop a
server by name, and never touch a server this skill did not launch, such as the owner's own
`omsync serve`.

## Rules

- Never point a run at `~/omsync-data` or the owner's config. The helper sets `OMSYNC_DATA_DIR` and
  `OMSYNC_CONFIG` inside `RUN`.
- Never weaken a journey to make it pass. A missing precondition goes in that feature's Gotchas.
- A failing journey on unchanged code is a finding: report it with the screenshot, and do not change
  product code unless the owner asks.
- Bank sign-in and receipt downloads need live validation at the institution (see AGENTS.md). This
  skill cannot run them; say so.
- When a page changes, update its feature file in the same piece of work.
