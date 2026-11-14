# Access and vault

## Purpose

Two permissions are separate: permission to use omsync, and permission to read bank logins from
the vault. You can read reports without an unlocked vault.

## Context

```mermaid
flowchart LR
    SignIn[Email and master password] --> Bitwarden
    Bitwarden --> App[Application session]
    Bitwarden --> Vault[Vault session]
    App --> Pages[Ledger and planning pages]
    Vault --> Logins[Bank logins] --> Connectors
    Keychain[macOS keychain] --> Bitwarden
```

## Flow

```mermaid
stateDiagram-v2
    [*] --> SignedOut
    SignedOut --> SignedIn: you give your email and master password, and Bitwarden accepts them
    SignedIn --> SignedOut: you sign out, 12 idle hours pass, or the server restarts
    state SignedIn {
        [*] --> Locked
        Locked --> Unlocked: you give the master password for a sync
        Unlocked --> Locked: the vault idle timeout ends
    }
    class SignedOut waits
    classDef waits fill:#fde68a,stroke:#b45309,stroke-width:2px,color:#1f1f1f
```

Sign-out also locks the vault.

## Design decisions

- **Bitwarden holds the only password.**
- **Secrets stay in memory.** This includes the master password and the vault session. API keys are
  in the macOS keychain. Logs contain stack frames and error types, not error messages.
- **Cookies are SameSite and checked by origin.** They are not Secure, because Secure cookies need
  HTTPS and the server uses HTTP.
- **The vault locks on its own timer.** A locked vault does not sign you out.

## Terms

- **Vault:** your Bitwarden account. It checks sign-in and holds bank logins.
- **Application session:** in memory. It ends after idle time or a server restart.
- **Vault session:** in memory, with its own idle timeout.
