Here’s a feature-by-feature verification plan, mapped to the new documentation. I’d keep this as a working review plan
 rather than add another permanent tracking document.

 1. How to inspect each feature

 For each feature:

 1. Read its spec. Decide whether the intended behavior and rationale make sense independently of the implementation.
 2. Follow its main journey. Inspect the UI or CLI behavior using a small, understandable dataset.
 3. Try a refusal case. Invalid input, conflicting evidence, stale data, or an unsafe operation should fail clearly
    without unintended changes.
 4. Check persistence and reruns. Reload, repeat the operation, and undo where supported.
 5. Record discrepancies. Distinguish:
     - Implementation defect: behavior violates the agreed design.
     - Design decision: the intended behavior needs reconsideration.
     - Documentation ambiguity: the contract is not clear enough.

 A passing test suite is supporting evidence, not proof that every statement in the spec is implemented correctly.

 2. Use a disposable dataset

 Start a separate terminal session for verification:

 ```sh
   export OMSYNC_DATA_DIR="$(mktemp -d /tmp/omsync-review.XXXXXX)"
   export OMSYNC_CONFIG="$OMSYNC_DATA_DIR/omsync.toml"
   export OMSYNC_PORT=8766

   uv run omsync db upgrade
 ```

 Use synthetic files from tests/fixtures/, not your personal statements. Keep this shell open for related CLI commands,
 then start the app with:

 ```sh
   uv run omsync serve
 ```

 Open http://127.0.0.1:8766.

 Authentication caveat: a disposable data directory isolates financial data, not authentication. Normal manual sign-in
 still uses your Bitwarden vault and configured keychain credentials. Automated tests use fake authentication. Run live
 institution checks separately and deliberately.

 3. Recommended review order and document mapping

 ### A. Foundations and access

 ┌───────┬────────────────────┬───────────────────────────────────────────────────────────────────────────────────────┐
 │ Order │ Feature and        │ What to inspect                                                                       │
 │       │ document           │                                                                                       │
 ├───────┼────────────────────┼───────────────────────────────────────────────────────────────────────────────────────┤
 │ 1     │ System — system.md │ Confirm product scope, local-only operation, data ownership, and separation of        │
 │       │                    │ fetching from financial writes. Inspect configuration refusal for a non-loopback      │
 │       │                    │ host.                                                                                 │
 ├───────┼────────────────────┼───────────────────────────────────────────────────────────────────────────────────────┤
 │ 2     │ Ledger — ledger.md │ Follow a statement row into a transaction. Confirm its payee is shown as stored and   │
 │       │                    │ source evidence remains intact. Verify account aliases preserve identity and splits   │
 │       │                    │ conserve signed amounts.                                                              │
 ├───────┼────────────────────┼───────────────────────────────────────────────────────────────────────────────────────┤
 │ 3     │ Changes —          │ Preview without mutation; apply once; retry without duplication; undo successfully.   │
 │       │ changes.md         │ Then create a later edit or dependency and confirm unsafe undo is refused.            │
 ├───────┼────────────────────┼───────────────────────────────────────────────────────────────────────────────────────┤
 │ 4     │ Access and vault — │ Sign in, sign out, and revisit a protected page. Verify errors never echo passwords.  │
 │       │ access.md          │ Inspect separate application/vault lifetimes, rate limiting, and cross-origin         │
 │       │                    │ protection through tests.                                                             │
 ├───────┼────────────────────┼───────────────────────────────────────────────────────────────────────────────────────┤
 │ 5     │ Interface —        │ Inspect light/dark/system themes, keyboard focus, validation feedback, narrow         │
 │       │ interface.md       │ layouts, privacy mode, and reduced motion. Reload to verify presentation preferences  │
 │       │                    │ persist.                                                                              │
 ├───────┼────────────────────┼───────────────────────────────────────────────────────────────────────────────────────┤
 │ 6     │ Settings —         │ Walk through institution setup, account mapping, connector selection, counterparties, │
 │       │ settings.md        │ self-names, and rental preferences. Confirm masked identifiers and that undo is       │
 │       │                    │ offered only for batch-backed settings.                                               │
 └───────┴────────────────────┴───────────────────────────────────────────────────────────────────────────────────────┘

 Useful targeted tests:

 ```sh
   uv run pytest -q tests/test_cli.py tests/test_installed_wheel.py
   uv run pytest -q tests/core/test_money.py tests/core/test_batches.py tests/ledger/test_migrations.py
   uv run pytest -q tests/core/test_vault.py tests/web/test_guard.py tests/web/test_sign_in.py
   uv run pytest -q tests/e2e/test_sign_in_shell.py
 ```

 ### B. Bringing in and understanding transactions

 ┌───────┬─────────────────────────┬──────────────────────────────────────────────────────────────────────────────────┐
 │ Order │ Feature and document    │ What to inspect                                                                  │
 ├───────┼─────────────────────────┼──────────────────────────────────────────────────────────────────────────────────┤
 │ 7     │ Statement import —      │ Import a file, repeat it, then import overlapping activity. Inspect              │
 │       │ import.md               │ join/create/review outcomes. Try identical purchases, changed identity evidence, │
 │       │                         │ unmapped accounts, and malformed input.                                          │
 ├───────┼─────────────────────────┼──────────────────────────────────────────────────────────────────────────────────┤
 │ 8     │ Transactions —          │ Edit category and memo by keyboard and pointer. Confirm payee is not editable.   │
 │       │                         │ Create and remove splits.                                                        │
 │       │ transactions.md         │ Exercise search, filters, saved filters, bulk selection, preview/apply, and      │
 │       │                         │ Needs review. Test a stale editor in two tabs.                                   │
 ├───────┼─────────────────────────┼──────────────────────────────────────────────────────────────────────────────────┤
 │ 9     │ Categories and keywords │ Create categories and conflicting keywords. Inspect Unique/Generic precedence    │
 │       │  — categories.md        │ and source-MEMO fallback. Preview categorization, apply, rerun, and undo.        │
 │       │                         │ Confirm manual and receipt classifications remain protected.                     │
 ├───────┼─────────────────────────┼──────────────────────────────────────────────────────────────────────────────────┤
 │ 10    │ Sync and connectors —   │ First inspect fake-connector behavior through tests: per-provider, sync --all,   │
 │       │ sync.md                 │ failures, operational logs, and competing-operation refusal. Then validate       │
 │       │                         │ actual institution behavior separately.                                          │
 ├───────┼─────────────────────────┼──────────────────────────────────────────────────────────────────────────────────┤
 │ 11    │ Receipts — receipts.md  │ Inspect Amazon purchases, delayed shipments, split charges, refunds, split       │
 │       │                         │ refunds, unknown payers, and gift-card review. Inspect Costco discounts, tax,    │
 │       │                         │ returns, and mixed cards. Apply only part of a receipt, resolve triage, and      │
 │       │                         │ rerun.                                                                           │
 ├───────┼─────────────────────────┼──────────────────────────────────────────────────────────────────────────────────┤
 │ 12    │ Reconciliation —        │ Inspect transfers, refunds, P2P, and joint settlements separately. Read the      │
 │       │ reconciliation.md       │ evidence before approval. Test ambiguity, rejection, manual linking, partial     │
 │       │                         │ coverage, and reruns after approval. Verify the bill anchor is not a settlement  │
 │       │                         │ side and partner contributions are not linked.                                   │
 └───────┴─────────────────────────┴──────────────────────────────────────────────────────────────────────────────────┘

 Useful targeted tests:

 ```sh
   uv run pytest -q tests/test_cli_import.py tests/ingest
   uv run pytest -q tests/web/test_transaction_routes.py tests/web/test_register_bulk.py
   uv run pytest -q tests/test_cli_categorize.py tests/ledger/test_categorize.py
   uv run pytest -q tests/test_cli_sync.py
   uv run pytest -q tests/test_cli_receipts.py tests/receipts
   uv run pytest -q tests/web/test_reconcile_routes.py tests/web/test_reconcile_matching.py tests/web/test_reconcile_auto.py tests/reconcile
 ```

 For hands-on procedures:

 - Import and review statements
 - Run sync and obtain receipts

 ### C. Reading results

 ┌───────┬────────────────────┬───────────────────────────────────────────────────────────────────────────────────────┐
 │ Order │ Feature and        │ What to inspect                                                                       │
 │       │ document           │                                                                                       │
 ├───────┼────────────────────┼───────────────────────────────────────────────────────────────────────────────────────┤
 │ 13    │ Accounts —         │ Compare displayed balances with statements. Inspect a clean balance interval, a       │
 │       │ accounts.md        │ discrepancy, and conflicting same-day balances. Rename, mark joint, close, and undo   │
 │       │                    │ without losing history.                                                               │
 ├───────┼────────────────────┼───────────────────────────────────────────────────────────────────────────────────────┤
 │ 14    │ Reports and        │ Calculate expected totals by hand for a small dataset. Include refunds, income        │
 │       │ exports —          │ reversals, excluded, uncategorized spending, and a partially linked transaction. Toggle │
 │       │ reports.md         │ joint share and inspect odd-cent split rounding. Compare yearly and rental workbooks  │
 │       │                    │ with full-account totals.                                                             │
 ├───────┼────────────────────┼───────────────────────────────────────────────────────────────────────────────────────┤
 │ 15    │ Dashboard —        │ Use the same period and joint-share setting as Reports. Compare summary figures,      │
 │       │ dashboard.md       │ spending-trend endpoints, and category totals. Include negative category nets.        │
 │       │                    │ Reorder/collapse sections and reload.                                                 │
 └───────┴────────────────────┴───────────────────────────────────────────────────────────────────────────────────────┘

 Useful targeted tests:

 ```sh
   uv run pytest -q tests/reports tests/web/test_reports_routes.py
 ```

 The important cross-feature test: the same money must agree across Reports, Home, and budget actuals. Differences in
 presentation must not introduce differences in accounting.

 ### D. Planning and recovery

 ┌───────┬─────────────────────┬──────────────────────────────────────────────────────────────────────────────────────┐
 │ Order │ Feature and         │ What to inspect                                                                      │
 │       │ document            │                                                                                      │
 ├───────┼─────────────────────┼──────────────────────────────────────────────────────────────────────────────────────┤
 │ 16    │ Budgets —           │ Build a month with fixed, non-monthly, flexible, and unassigned categories. Verify   │
 │       │ budgets.md          │ the flex formula, income override, goal deductions, positive/negative rollover, and  │
 │       │                     │ mode switching without data loss. Compare actuals with Reports.                      │
 ├───────┼─────────────────────┼──────────────────────────────────────────────────────────────────────────────────────┤
 │ 17    │ Goals — goals.md    │ Create linked and unlinked goals. Compare progress with the account’s reported       │
 │       │                     │ balance and date. Try zero contribution, completed targets, target dates, archive,   │
 │       │                     │ and undo. Confirm planned contributions do not create actual savings.                │
 ├───────┼─────────────────────┼──────────────────────────────────────────────────────────────────────────────────────┤
 │ 18    │ Recurring series —  │ Inspect suggestions against their supporting transactions. Confirm, dismiss, rerun,  │
 │       │ recurring.md        │ restore, and edit. Inspect month-end schedules and paid/upcoming/missed occurrences. │
 │       │                     │ Confirm expectations never become ledger transactions.                               │
 ├───────┼─────────────────────┼──────────────────────────────────────────────────────────────────────────────────────┤
 │ 19    │ Backups and         │ Archive the disposable dataset, make later changes, verify without replacement, then │
 │       │ recovery —          │ restore. Confirm source files return and later state is preserved in a safety        │
 │       │ backups.md          │ archive. Test damaged input and operation-lock refusal.                              │
 └───────┴─────────────────────┴──────────────────────────────────────────────────────────────────────────────────────┘

 Useful targeted tests:

 ```sh
   uv run pytest -q tests/plan
   uv run pytest -q tests/test_cli_backup.py
 ```

 Follow Back up and restore for the manual recovery drill. Never perform the drill against your real data directory.

 4. Finish with an integrated journey

 After inspecting the features individually, follow one complete story:

 Import → resolve review → categorize → apply receipts → reconcile → inspect accounts/reports/dashboard → budget and
 goals → backup and restore.

 At the end, verify that:

 - Every counted amount is explainable from the source evidence.
 - Manual choices survived automation.
 - Repeated operations did not duplicate money or links.
 - Partial application did not prevent completing the remaining work.
 - Restore recovered both the ledger and its evidence.

 That integrated pass is where inconsistencies between otherwise reasonable feature designs are most likely to surface.