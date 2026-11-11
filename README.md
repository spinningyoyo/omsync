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
`omsync doctor` reports missing setup. See [interface and settings](docs/architecture/interface.md) for
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

### Sign-in scenes

The sign-in page grows a pixel-art scene. The scenes, the Nursery Studio that draws and edits
them, and their history live in a sibling repository, `../omsync-nursery`, so this history stays
about the app. omsync installs the `omsync-nursery` package from the nursery's `release` branch.
`uv.lock` records the exact commit. omsync requires a nursery version range (`>=0.1.1,<0.2`); a release outside it is a contract change, which needs host code and a wider range in the same commit. omsync serves the scene files, including `scenes.css`, at
`/static/vendor/nursery/`.

To take new art after a release in the nursery (`uv run nursery release` there), run one command
here. It locks the newest release, runs the checks and tests, and commits:

```sh
sh scripts/upgrade-nursery.sh
```

To see art in progress, before a release, run omsync on the live nursery checkout. It shows colour
changes too, not only the art:

```sh
sh scripts/dev-nursery.sh serve
```

## License

AGPL-3.0. omsync includes code adapted from Sure (AGPL-3.0).
Required source notices remain with adapted files; vendored asset notices remain beside
the assets. See [LICENSE](LICENSE).
