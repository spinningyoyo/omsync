# omsync

A local, single-user personal finance tracker for a Mac. Import institution statements,
categorize spending down to receipt items, explain related transactions, and plan budgets
and goals. The name plays on ออมสิน (om sĭn), Thai for saving money.

## Setup

### Try the demo

The demo needs only Python 3.12 or later and [uv](https://docs.astral.sh/uv/). It is a made-up
person's year of money, built on your Mac in a temporary directory that is deleted when the
server stops, so no ledger, Bitwarden account, or bank login is needed.

```sh
uv sync
uv run omsync serve
```

Open `http://127.0.0.1:8765` and click **Explore the demo**. `omsync serve` shows the demo
whenever there is no ledger yet, and `uv run omsync serve --demo` shows it even when there is one.

### Your own ledger

Requires Python 3.12 or later, [uv](https://docs.astral.sh/uv/), the Bitwarden CLI (`bw`),
and access to the macOS keychain. Website connectors also use Chrome.

```sh
uv sync
uv run keyring set omsync bitwarden-client-id
uv run keyring set omsync bitwarden-client-secret
uv run omsync db upgrade
uv run omsync doctor
uv run omsync serve
```

The keyring commands prompt for the API client ID and secret from your Bitwarden account.
Do not put credentials in a config file or shell command arguments. Sign in to omsync with
your vault email and master password.

Open `http://127.0.0.1:8765`. With a ledger, the server upgrades the database before starting.
`uv run omsync db upgrade` creates a ledger; until then, `omsync serve` runs the demo instead.
It listens on this Mac only. Personal data defaults to `~/omsync-data`, outside the repository.

Optional machine configuration lives at `~/.config/omsync/omsync.toml`:

```toml
data_dir = "~/omsync-data"
port = 8765
email = "you@example.com"
vault_idle_minutes = 15
```

`OMSYNC_CONFIG` selects a different config file. `OMSYNC_*` settings override its values.
`omsync doctor` reports missing setup. See [settings](docs/architecture/settings.md) for
configuration ownership and [access](docs/architecture/access.md) for credential handling.

## Documentation

[docs/README.md](docs/README.md) indexes feature architecture and operating guides.
Start with [system](docs/architecture/system.md), [ledger](docs/architecture/ledger.md),
and [changes](docs/architecture/changes.md).

## Development

```sh
uv run pytest -q
uv run ruff check .
bash scripts/build-css.sh        # after style, template, or controller changes
uv build --wheel
```

The wheel includes migrations and web assets. The slow packaging test installs and exercises
it in a disposable environment; `uv run pytest -m "not slow"` omits that test. Use synthetic
data and temporary directories for development and testing. See [AGENTS.md](AGENTS.md).

### Sign-in forest scenes

The sign-in page grows a pixel-art scene; each scene is a module in
`src/omsync/web/static/js/forest/`. Review a scene and fix its pixels in the forest studio:

```sh
uv run python scripts/forest-studio.py    # opens http://127.0.0.1:8766
```

1. **Review.** Pick a scene. The live scene runs at the top, with a stage slider, Water, and
   desktop, phone and laptop sizes. Below it, a card for every plant, landmark, animal, bird and
   effect shows each pose in light and dark, its animation frames, its size and size tier, and
   the line of code that draws it (click to copy). The studio draws from the code on disk:
   **Reload code** shows a change.
2. **Edit.** Click a sprite to open the pixel editor. Pencil, eraser and picker use the scene's
   palette, and light and dark are painted together, each in its own colour. **Save edit** writes
   a patch to `.forest-studio/<scene>/edits/`, which git ignores: every changed pixel with its
   offsets and palette keys, and a picture of the original and the edit.
3. **Apply.** Ask the agent to apply the edits. It changes the drawing code, then runs
   `uv run python scripts/forest-studio.py check <scene>`, which compares what the code draws
   with each edit and reports it as waiting, in code, or differs, with the pixels that differ.
4. **Review the result.** **Changes** shows each edit beside what the code draws now, and every
   sprite that moved without an edit, such as another walk frame or the young. When you are
   happy, **Accept and clear** resets the scene's edits.

**Trigger** under the live scene fires its timed events now (a fly-by, the hawk's stoop, an
eruption, the next meteor shower) instead of waiting for them. An event still waits for its stage,
for night, or for its animal to be on the page.

**Proposals.** Agents post new or changed art, still or animated, to the studio instead of showing
pictures in chat. A proposal is a small module in `.forest-studio/<scene>/proposals/`. The studio
picks it up within a few seconds, from any agent session, and the scene picker shows how many wait
for review. **Review** shows today's sprite beside each variant in light and dark, with play,
frame-by-frame steps and slow motion, and plays the chosen variant over the live scene; **Day /
night** switches both. Choose a variant, add notes, or fix pixels on any frame with **Edit
pixels**, then **Approve choice** or **Request changes**. Tell the agent: it reads your review
with `uv run python scripts/forest-studio.py proposals`, revises the proposal (it shows as waiting
for review again), or builds the approved variant into the scene. The forest-scene skill documents
the proposal format for agents.

[docs/forest-scenes.md](docs/forest-scenes.md) lists every element and the code that draws it.
Rebuild it with `uv run python scripts/forest-catalog.py`; never pass `--only`, which leaves only
those scenes in the doc.

## License

AGPL-3.0. omsync includes code adapted from Sure (AGPL-3.0) and Actual Budget (MIT).
Required source notices remain with adapted files; vendored asset notices remain beside
the assets. See [LICENSE](LICENSE).
