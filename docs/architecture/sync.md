# Sync, providers, and connectors

## Purpose

Sync downloads the statement files that you could download by hand, then imports them. Download
and import are separate, so a bank's website change never changes import rules.

## Context

```mermaid
flowchart LR
    Vault[Bitwarden vault, optional] --> Connector
    You[You sign in by hand] -.->|no vault| Connector
    Bank[Bank website in Chrome] <--> Connector
    Connector --> Files["statements/INSTITUTION/"]
    Files --> Import
    Import --> Auto[Automatic step]
    Connector --> Log[Sync log]
```

## Flow

### One sync

```mermaid
flowchart TD
    Start[Sync a provider] --> Lock[Take the operation lock]
    Lock --> HasVault{Vault?}
    HasVault -->|Yes| Vault[Read the bank login and type it]
    HasVault -->|No| Manual[You sign in, then select I have signed in]
    Vault --> SignIn[Sign in]
    Manual --> Fetch
    SignIn --> Fetch[Download each account's statements]
    Fetch --> Keep[Keep the files]
    Keep --> Import[Import each file]
    Import --> Auto[Counterparty, categorize, link]
    Auto --> Done[Release the lock]
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

- **Sign-in is complete when the bank returns the account list.** omsync does not read the sign-in
  state from the page. A redesign cannot hide a sign-in or show a false one.
- **Without a vault, you tell omsync that you signed in.** omsync then asks the bank for the
  account list, as after a typed login.
- **Files are kept as the bank sent them.** omsync never converts or merges them, because merged
  files caused duplicates in maneko.
- **Each download overlaps the one before it.** Banks post some transactions late with an earlier
  date. Import joins the repeated rows.
- **A new statement number links automatically only when one account matches it.** Otherwise you
  choose the account.
- **Sync uses its own Chrome profile in the data directory**, not your usual profile. A debugging
  port on 127.0.0.1 is open while a sync runs.
- **Bank sync is tested on macOS with Google Chrome.** The code has paths for other systems, which
  are not tested.

## Terms

- **Provider:** one bank sign-in, its connector, and the accounts it fetches. One bank can have
  several providers.
- **Connector:** the scraper for one bank.
- **Sync all:** each enabled provider in turn. A failure does not stop the next one. Cancel does.

See [ADR 0002](../adr/0002-download-statements-instead-of-using-an-aggregator.md) for why omsync
scrapes. See [bank requests](bank-requests/README.md) for each bank's download requests.
