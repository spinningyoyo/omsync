# Changelog

This file lists the changes that matter to a person who runs omsync. It does not list changes to
the linters, the checks, the tests or the CI. Git history has them.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). The version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-21

### Added

- Support for Windows.
- Store sync for Amazon and Costco, on the Data sync page.
- A Receipts page.
- Tags.
- Bank sync without a password manager.
- `omsync config show` and `omsync config set`.

### Changed

- Sign-in uses the computer's owner check (Touch ID, Windows Hello, or login password) instead of
  a Bitwarden account.
- Bitwarden is optional. Connect or disconnect it on the Data sync page.
- Amazon receipts come from your Amazon payment history instead of the data export.
- An Amazon charge with no card counts as a gift card or points.
- Bank sync is now Data sync, with tabs for banks and stores.
- `omsync sync --all` also syncs stores.
- Costco sync opens Chrome by itself.
- Rental properties are set on accounts and categories.

### Fixed

- Sync all blocks other syncs until it finishes.

### Removed

- Reading the Amazon data export (`Order History.csv`, `Refund Details.csv`).

## [0.1.0] - 2026-11-14

### Added

- A local personal finance tracker for one person on one Mac. It listens on `127.0.0.1` only, and
  sign-in uses your Bitwarden account.
- A demo with a made-up person's year of money. It needs no ledger, no Bitwarden account and no
  bank login.
- Statement import, and bank sync for Bank of America, Chase, Citi and Fidelity Rewards.
- Receipt engines for Amazon and Costco that split a charge into categories.
- A register with splits, bulk changes and undo. Categories come from keywords and rules, and each
  automatic choice can be explained and changed.
- Reconciliation of transfers, card payments, refunds and joint bills, and contacts with loan
  balances.
- Budgets by category and by flexible pool, goals, and recurring bills and paychecks.
- Reports, account pages, backup and restore.
- Sign-in scenes from the `omsync-nursery` package.

[Unreleased]: https://github.com/spinningyoyo/omsync/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/spinningyoyo/omsync/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/spinningyoyo/omsync/releases/tag/v0.1.0
