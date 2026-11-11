"""Transfer matching must not auto-match an ambiguous case. Synthetic transactions only."""

from datetime import date

from omsync.reconcile.rules.transfers import rule_transfer_same_confirmation

ON = date(2026, 3, 2)
CONF = "ab12cd34ef"
# ── Transfer with the same confirmation number ──────────────────────────────────────────────


def test_transfer_same_confirmation_skips_a_same_day_same_amount_triple_as_ambiguous(
    add_txn, pool_for, run_rule
):
    add_txn("checking", ON, "-200.00", f"Online transfer to CHK 2222 Conf# {CONF}")
    add_txn("savings", ON, "200.00", f"Online transfer from CHK 1111 Conf# {CONF}")
    add_txn("card", ON, "200.00", f"Online transfer from CHK 1111 Conf# {CONF}")
    pool = pool_for()

    assert run_rule(rule_transfer_same_confirmation, pool).drafts == []
    assert len(pool) == 3


# ── Zelle with the same confirmation number ─────────────────────────────────────


# ── Credit card bill payment ──────────────────────────────────────────────


# ── Credit card credit balance refunded ────────────────────────────────────


# ── Transfer with the same amount and date, no confirmation ─────────────────
