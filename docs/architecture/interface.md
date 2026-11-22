# Interface and settings

## Purpose

The server renders each page and checks each edit. This page also says where each kind of setting
is stored.

## Context

```mermaid
flowchart LR
    Server[Server: validation and HTML] --> Browser
    Browser -->|request or edit| Server
    TOML[omsync.toml: machine] --> Server
    DB[(Database: what you track)] --> Server
    Local[Browser storage: theme, privacy, panel size] --> Browser
    Nursery[omsync-nursery package: sign-in scenes, mascot, favicons] --> Browser
```

## Flow

### One edit

```mermaid
sequenceDiagram
    participant You
    participant Browser
    participant Server
    You->>Browser: Edit a field
    Browser->>Server: Send the field and its version
    Server-->>Browser: Checked HTML for that region
    Browser->>Browser: Swap the region and put focus back
    Browser-->>You: The result, or the error with your input kept
```

### Add a bank

```mermaid
flowchart LR
    Bank[Institution and bank] --> Login[Vault login, when a vault is connected]
    Login --> Accounts[Accounts to fetch]
    Accounts --> Sync[Sync, then find files]
    Sync --> Map[Match statement accounts]
    Map --> Import[Preview and import]
```

### Add a store

```mermaid
flowchart LR
    Store[Amazon or Costco] --> Name[Name of the login]
    Name --> Login[Vault login, when the store uses one]
    Login --> Card[Store card on the Receipts tab]
```

## Design decisions

- **Each setting has one location.** Machine settings are in `omsync.toml`. Tracked data is in the
  database. The look on each device is in browser storage. For secrets, see [access](access.md).
- **The vault is set where it is used, on the Data sync page.** It has no Settings page of its
  own.
- **Data sync has one tab for banks and one for stores.** Each provider is a card with Sync, Edit,
  and Delete. Sync all is in the page header.
- **Privacy mode blurs amounts on the screen.** It is not access control. Exports are not blurred.
- **Pages show only the last four digits of an account number.** The full number does not go to the
  browser. Account mapping uses short-lived tokens.
- **The sign-in art is in a separate repository, `omsync-nursery`.** Art changes do not appear in
  the omsync history. Only a contract change touches both repositories.

## Terms

- **Rail:** the icon navigation on the left. It becomes bottom navigation on a phone.
- **Accounts panel:** the list of accounts beside the page.
- **Guides:** short walkthroughs in Settings. Read marks stay in this browser.
