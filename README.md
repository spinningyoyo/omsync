# omsync

omsync is a local, single-user personal finance tracker for a Mac. You import institution
statements, sort spending down to receipt items, explain related transactions, and plan budgets and
goals. The name plays on ออมสิน (om sĭn), Thai for saving money.

> Pull requests are by invitation only. Open an issue first. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Getting started

You need Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

### Try the demo

The demo needs no ledger, no Bitwarden account and no bank login. It is a made-up person's year of
money. omsync builds it in a temporary directory and deletes the directory when the server stops.

```sh
git clone https://github.com/spinningyoyo/omsync
cd omsync
uv sync
uv run omsync serve --demo
```

Open `http://127.0.0.1:8765` and click **Explore the demo**. Without `--demo`, `omsync serve` also
shows the demo while there is no ledger.

### Use your own ledger

You also need the Bitwarden CLI (`bw`), access to the macOS keychain, and Chrome. Chrome is for the
bank connectors.

1. Install `bw`. With Homebrew, run `brew install bitwarden-cli`. Other ways to install it are in
   the [Bitwarden CLI documentation](https://bitwarden.com/help/cli/).
2. Get your Bitwarden personal API key. Sign in to the Bitwarden web vault. Open **Settings**, then
   **Security**, then **Keys**. Click **View API key** and enter your master password. The page
   shows a `client_id` and a `client_secret`.
3. Store both in the macOS keychain. Each command asks for the value:

   ```sh
   uv run keyring set omsync bitwarden-client-id
   uv run keyring set omsync bitwarden-client-secret
   ```

4. Create the ledger, check the setup and start the server:

   ```sh
   uv run omsync db upgrade
   uv run omsync doctor
   uv run omsync serve
   ```

`omsync doctor` lists what is missing. Do not put credentials in a config file or in command
arguments.

Open `http://127.0.0.1:8765` and sign in with your Bitwarden email and master password. omsync has
no password of its own. After the first sign-in:

1. Go to **Settings**, then **Guides**, for five short guides to the app.
2. To fetch statements from a bank, add a provider in **Settings**, then **Bank sync**.
3. For Amazon and Costco receipts, see [Get receipts](docs/how-to/get-receipts.md).
4. Make a backup. See [Back up and restore](docs/how-to/back-up-and-restore.md).

Your data lives in `~/omsync-data`, outside the repository.

### Config file

The config file is optional. It is `~/.config/omsync/omsync.toml`:

```toml
data_dir = "~/omsync-data"
port = 8765
email = "you@example.com"
vault_idle_minutes = 15
```

`OMSYNC_CONFIG` selects another file. `OMSYNC_*` settings override its values. See
[interface and settings](docs/architecture/interface.md) for where each setting lives.

## Security model

The server listens on `127.0.0.1` only, and sign-in uses your Bitwarden account. Secrets stay in
memory and in the macOS keychain. See [access and vault](docs/architecture/access.md) and
[sync](docs/architecture/sync.md).

## Development

```sh
uv sync
git config core.hooksPath .githooks   # once for each clone: it runs the checks before each commit
sh scripts/check.sh                   # ruff, Biome (JS and CSS) and import layers
uv run playwright install chromium    # once: the browser tests need it
uv run pytest -q                      # the end-to-end tests in tests/e2e
uv run pytest -m "not slow"           # skip the test that builds and installs the wheel
bash scripts/build-css.sh             # after you change template classes
uv build --wheel
```

Use synthetic data and temporary directories. Never use your real ledger. [AGENTS.md](AGENTS.md)
has the rules and the review standards.

Supply chain:

- `uv.lock` pins every Python package and records its hashes. CI runs `uv sync --locked`.
- The CI workflow pins each GitHub Action by commit.
- `scripts/build-css.sh` downloads one pinned Tailwind CLI and checks its SHA-256 digest before it
  runs it.
- [VENDOR.md](src/omsync/web/static/vendor/VENDOR.md) pins the vendored web files with their
  checksums.

## Sign-in scenes

The sign-in scenes come from the `omsync-nursery` package, in the sibling repository
`../omsync-nursery`. [AGENTS.md](AGENTS.md) has the release rules.

```sh
sh scripts/upgrade-nursery.sh     # take the newest release, run the checks and tests, and commit
sh scripts/dev-nursery.sh serve   # run omsync on your nursery checkout to see art before a release
```

## Documentation

[docs/README.md](docs/README.md) indexes the architecture pages and the guides. Start with
[system](docs/architecture/system.md), [ledger](docs/architecture/ledger.md) and
[changes](docs/architecture/changes.md).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) first. [AGENTS.md](AGENTS.md) has the rules for agents and
for people who work in the repository. [CHANGELOG.md](CHANGELOG.md) lists the changes.

## Licence

omsync is licensed under the [AGPL-3.0-only](LICENSE). Copyright (C) 2026 spinningyoyo.

omsync includes code adapted from [Sure](https://github.com/we-promise/sure) (AGPL-3.0, itself from
Maybe Finance). The adapted files keep their source notices.

omsync also includes third-party files under their own licences: Lucide icons (ISC), htmx (0BSD),
Stimulus (MIT), D3 (ISC), d3-sankey (BSD-3-Clause), Tailwind CSS (MIT), and the Geist and Noto Sans
Thai fonts (SIL OFL 1.1). [VENDOR.md](src/omsync/web/static/vendor/VENDOR.md) lists them and links
the licence texts.

The bank logos in `src/omsync/web/static/img/institutions` are trademarks of their owners. omsync
has no affiliation with them.
