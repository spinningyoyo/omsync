# Sync, providers, and connectors

## Purpose

Sync downloads the files that you could download by hand. A bank provider downloads statement
files, and omsync then imports them. A receipt provider downloads receipt files, and omsync then
reads them. Download and import are separate, so a website change never changes import rules.

## Context

```mermaid
flowchart LR
    Vault[Bitwarden vault, optional] --> Connector
    You[You sign in by hand] -.->|no vault| Connector
    Site[Bank or store website in Chrome] <--> Connector
    Connector --> Statements["statements/INSTITUTION/"]
    Connector --> Receipts["receipts/VENDOR/PROVIDER/"]
    Statements --> Import
    Import --> Auto[Automatic step]
    Receipts --> Read[Read receipts]
    Connector --> Log[Sync log]
```

## Flow

### Sync all

```mermaid
flowchart TD
    Start[Sync all] --> Lock[Take the sync lock]
    Lock --> Banks[Each turned-on bank provider]
    Banks --> Stores[Each receipt provider]
    Stores --> Done[Release the lock]
    Banks -.->|one fails| Banks
    Stores -.->|one fails| Stores
```

The page and the `omsync sync --all` command use the same sync. A failure stops only its provider.
Cancel stops the providers that did not start.

### One bank sync

```mermaid
flowchart TD
    Start[Sync a bank provider] --> HasVault{Vault?}
    HasVault -->|Yes| Vault[Read the bank login and type it]
    HasVault -->|No| Manual[You sign in, then select I have signed in]
    Vault --> SignIn[Sign in]
    Manual --> Fetch
    SignIn --> Fetch[Download each account's statements]
    Fetch --> Keep[Keep the files]
    Keep --> Import[Import each file]
    Import --> Auto[Counterparty, categorize, link]
    class Manual waits
    classDef waits fill:#fde68a,stroke:#b45309,stroke-width:2px,color:#1f1f1f
```

### One receipt sync

```mermaid
flowchart TD
    Start[Sync a receipt provider] --> SignIn[Sign in to the store in Chrome]
    SignIn --> Fetch[Download new and changed receipts]
    Fetch --> Read[Read every receipt file of the provider]
    Read --> Held{Another provider has the receipt?}
    Held -->|Yes| Leave[Keep it with that provider]
    Held -->|No| Save[Save it for this provider]
    class SignIn waits
    classDef waits fill:#fde68a,stroke:#b45309,stroke-width:2px,color:#1f1f1f
```

### Sign-in with a vault

```mermaid
stateDiagram-v2
    [*] --> Watching: the connector types the login
    Watching --> SignedIn: the bank answers the account list
    Watching --> Captcha: a CAPTCHA appears
    Watching --> Identity: an identity check appears
    Watching --> NeedsYou: nothing happens for a minute
    Captcha --> Watching: you solve it
    Identity --> EnterCode: the bank offers a code
    Identity --> ScanQR: only an app approval is offered
    EnterCode --> Watching: you enter the code
    ScanQR --> Watching: you approve in the app
    SignedIn --> [*]
    NeedsYou --> [*]
    class Captcha,EnterCode,ScanQR waits
    classDef waits fill:#fde68a,stroke:#b45309,stroke-width:2px,color:#1f1f1f
```

## Design decisions

- **One sync runs at a time on this computer.** Sync all holds the sync lock from the first
  provider to the last. A sync from the terminal cannot start between two providers.
- **Banks and stores follow the same provider rules.** Both kinds have one key space. Add, edit, and
  delete are batches, and you can undo them. omsync refuses an edit or a delete while a sync runs.
- **Deleting a provider keeps your data.** Accounts, transactions, statement files, and receipt
  files stay. omsync deletes the sync log of the provider and its receipt rows. A new sync reads
  the receipt files again.
- **Sign-in is complete when the bank returns the account list.** omsync does not read the sign-in
  state from the page. A redesign cannot hide a sign-in or show a false one.
- **Without a vault, you tell omsync that you signed in.** omsync then asks the bank for the
  account list, as after a typed login.
- **Files are kept as the bank sent them.** omsync never converts or merges them, because merged
  files caused duplicates in maneko.
- **Each download overlaps the one before it.** Banks post some transactions late with an earlier
  date. Import joins the repeated rows. A receipt sync starts 14 days before the newest receipt.
- **A new statement number links automatically only when one account matches it.** Otherwise you
  choose the account.
- **Sync uses its own Chrome profile in the data directory**, not your usual profile. A debugging
  port on 127.0.0.1 is open while a sync runs.
- **Bank sync is tested on macOS with Google Chrome.** The code has paths for other systems, which
  are not tested.

## Terms

- **Provider:** one sign-in and its connector. A bank provider fetches statements for its accounts.
  One bank can have several providers. A receipt provider fetches the receipts of one store login.
- **Connector:** the scraper for one bank or one store.
- **Sync all:** each turned-on bank provider in turn, then each receipt provider.
- **Sync log:** the start, the end, the status, and the file or receipt count of each provider sync.

See [ADR 0002](../adr/0002-download-statements-instead-of-using-an-aggregator.md) for why omsync
scrapes. See [bank requests](bank-requests/README.md) and
[vendor requests](vendor-requests/README.md) for the download requests.
