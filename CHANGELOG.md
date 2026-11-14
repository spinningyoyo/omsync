# Changelog

This file lists the changes that matter to a person who runs omsync. It does not list changes to
the linters, the checks, the tests or the CI. Git history has them.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). The version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-13

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

[Unreleased]: https://github.com/spinningyoyo/omsync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/spinningyoyo/omsync/releases/tag/v0.1.0
