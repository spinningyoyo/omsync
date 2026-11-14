# Fidelity Rewards requests

## Purpose

The Fidelity Rewards card runs on a partner bank's platform. Its download needs a bearer token
that the page keeps in session storage. Last validated live: 2026-11-07. Not tested live: more
than one account.

## Context

The page does not request an account list. The sign-in signal is session storage: it holds
`AccessToken`, `multiAccountMetaData`, and `PartnerConfig`. The account token arrives about two
seconds after the dashboard loads.

## Flow

```mermaid
sequenceDiagram
    participant C as Connector
    participant T as Signed-in tab
    participant F as Fidelity Rewards
    C->>T: Read AccessToken, the account token, and PartnerConfig
    C->>F: POST account token, dates, QFX, with Bearer and partner headers
    F-->>C: QFX
```

## Design decisions

- **Use `multiAccountMetaData`, not the selected-account value.** The site deletes the selected card
  after a download.
- **An end date of today becomes today plus four days.** The site sends this value.
- **The path contains GraphQL, but the body is plain JSON.**

## Terms

- **Bearer token:** a token that the request sends in its `Authorization` header.
- **Partner headers:** headers that name the partner bank's platform.
