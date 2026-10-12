# Settings

## Purpose and design philosophy

Settings separate financial configuration from machine configuration and presentation.
The user should know where a decision lives and whether it can be undone. Sure inspires
the settings navigation, not a hosted account or subscription model.

## Configuration ownership

Machine settings live in `omsync.toml`, normally at `~/.config/omsync/omsync.toml`.
`OMSYNC_CONFIG` selects another file; `OMSYNC_*` values override machine settings.
The defaults supply the data directory, loopback host, port, and vault idle timeout. The
vault email can be configured so terminal operations need not ask for it every time.
Configuration cannot widen the loopback bind address. Secrets live in the
[keychain and vault](access.md), never in TOML.

Institutions, accounts, aliases, providers, categories, keywords, amount rules, and
counterparties live in the database. Browser storage owns device presentation such as
theme, privacy mode, and accounts-panel geometry. Database presentation preferences own
shared choices such as joint share, dashboard layout, and whether a failed
sync closes its browser. These are not competing sources
of financial truth.

## Bank sync

Bank sync lists each institution by name and logo, with its providers and the accounts they fetch. A
[provider](sync.md#providers) is a bank login; an institution may have several, and each fetches
only its own accounts. A provider card shows its bank, its vault login by name while the vault is
unlocked, its latest sync, and its accounts. Open accounts that no provider fetches are listed under
their institution as not synced. Statement files for them still import from the institution's folder.
Logos are bundled files named for institution keys; a business variant such as `chase_business`
shares its personal institution's logo, and an institution without a file shows its initial. Sync
all and each card's Sync button start syncs from the page; see [syncing from the web](sync.md#syncing-from-the-web).

Editing a provider changes its vault login and the accounts it fetches. The account choices name
the provider that already fetches an account, and that account cannot be chosen twice. The bank is
read-only: to use another bank, delete the provider and add one. Deleting keeps the accounts,
transactions, statement files, and sync history, and only stops syncing. Both are
[batches](changes.md) that undo reverses, and both are refused while a sync holds the operation lock.

The add-provider wizard has five steps:

```mermaid
flowchart TD
    Setup[Institution, provider key, and bank] --> Vault[Select a vault login]
    Vault --> Accounts[Configure accounts to fetch]
    Accounts --> Fetch[Sync from Bank sync or the terminal, then discover files]
    Fetch --> Map[Map discovered statement accounts]
    Map --> Preview[Preview import]
    Preview --> Apply[Apply the reviewed import]
```

The first step accepts an existing institution key, which adds a provider to that institution and
keeps its name. A new key creates the institution with the display name. The provider key must be new
and defaults to the institution key. The provider key then travels in hidden fields, and the vault
login and accounts steps save for that provider. The drop folder is not offered as a bank. Several
institutions may use one connector when they share the same bank flow, each with its own vault
login, such as personal and joint Chase logins. Chase Business selects its distinct connector
because its authentication and download contract differ.

A new account made outside the wizard, in the mapping step or with `omsync accounts add`, joins its
institution's only provider. When the institution has several providers, the account stays unassigned
and shows as not synced until Edit provider chooses it.

Account mapping offers a new account, an alias for an existing account, or skip. A matching
last-four label can suggest an account but does not prove its identity, so only a sync links
one by itself, under the conditions in [sync](sync.md); otherwise the person confirms. Bank of America's
CSV folders use last four digits as statement account IDs. Import binds to the exact files
and proposed changes shown in the preview; changed evidence produces a new preview rather
than an unreviewed apply.

Full statement-account identifiers do not belong in HTML, URLs, or messages. Mapping uses
short-lived opaque tokens tied to the institution; forged, expired, or foreign tokens map
nothing. Public labels show only masked last four digits. Source files may contain full
identifiers and remain private data.

## Matching and rental settings

Counterparties have names, person/business kind, and outgoing/incoming patterns for
[reconciliation](reconciliation.md), and may be marked as expecting no match: the standing
acknowledgment reviewed and removed here. The user's own names are literal transfer-recognition
alternatives, not regexes or hard-coded personal identities.

Rental preferences map accounts to property labels and choose category names for
[workbooks](reports.md). Removing a property mapping changes reporting configuration,
not its transactions. Category choices outside the configured set appear as Other.

Institution, account, provider, and counterparty changes use [batches](changes.md).
Self-names, rental mappings/categories, and ordinary presentation preferences save directly
without financial undo. The interface must not offer an undo promise for
settings that are not batches.

## Design rationale

The wizard validates each submitted step on the server. Hidden form values, including the
provider key, are input, not authority. Opaque mapping tokens let the user approve source identity without exposing
full account numbers to browser markup or allowing a forged ID to select another account.

Machine configuration describes where omsync runs. Database configuration describes what
it tracks. Keeping those distinct makes local operation, tests, and
[backup restoration](backups.md) predictable. `omsync doctor` reports missing setup without
starting an import or an external sync.
