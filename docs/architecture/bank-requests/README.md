# Reconstructing bank requests

## Purpose

A connector sends the request that the bank's download button sends. A direct request survives a
page redesign. This page shows how to find that request and prove that it works. Each bank page
gives the date of its last live test.

## Context

```mermaid
flowchart LR
    Record[scripts/bank_record.py] --> Capture[Capture: temporary, private]
    Capture --> Trace[scripts/bank_trace.py]
    Trace --> Finding[Bank page in this folder]
    Finding --> Connector[Connector code]
```

| Bank | Token the download needs | Where it comes from |
|---|---|---|
| [Bank of America](boa.md) | None, only an account key | The account overview |
| [Chase](chase.md) | `csrftoken` | A token endpoint |
| [Citi](citi.md) | None, only the `client_id` cookie as a header | The session cookies |
| [Fidelity Rewards](fidelity.md) | Bearer token | Browser session storage |

## Flow

```mermaid
flowchart TD
    subgraph Find ["1. Find the request"]
        Record[Record one real download] --> Request[Find the request that returned the file]
        Request --> Sort{What is each value?}
        Sort -->|Constant, date, or new UUID| Rule[Note how to make it]
        Sort -->|Long and opaque| Search[Search earlier traffic for the exact value]
        Search -->|Not found| Script[Read the script that sent it]
        Search -->|Found| Source[Note the first source]
        Script --> Source
    end
    subgraph Prove ["2. Prove it"]
        Replay[Rebuild and replay from a signed-in tab] --> Same{Same bytes as the clicked file?}
        Same -->|Yes| Vary[Change one input at a time]
    end
    Rule --> Replay
    Source --> Replay
    Same -->|No| Request
    Vary --> Write[Write the finding on the bank page]
```

## Design decisions

- **Find a value by its content, not its name.** Names can be wrong. Bank of America has a
  `getCSRFToken` endpoint that downloads do not use. Chase's `x-jpmc-csrf-token` header contains
  `NONE`.
- **A success status is not proof.** A bank can send an error with status 200. Compare the bytes.
- **Use only a test that can fail.** A narrow date range must return fewer rows.
- **Keep secrets out of captures.** Use a temporary browser profile. Do not save sign-in or MFA
  bodies. Print names and lengths, not values. Delete the capture after use. Send few replays,
  because repeated failures can start the bank's fraud checks.

## Terms

- **Capture:** a recording of a download's traffic, cookies, and storage.
- **Replay:** the rebuilt request sent from a signed-in tab.
- **Decoy:** a value that looks important but holds a placeholder.
