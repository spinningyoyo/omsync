# omsync

A local, single-user personal finance tracker for a Mac. Import institution statements,
categorize spending down to receipt items, explain related transactions, and plan budgets
and goals. The name plays on ออมสิน (om sĭn), Thai for saving money.

## Setup

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

Open `http://127.0.0.1:8765`. The server creates or upgrades the database before starting.
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

## License

AGPL-3.0. omsync includes code adapted from Sure (AGPL-3.0) and Actual Budget (MIT).
Required source notices remain with adapted files; vendored asset notices remain beside
the assets. See [LICENSE](LICENSE).
