"""Transfer matching must not auto-match an ambiguous case. Synthetic transactions only."""

from datetime import date

from omsync.reconcile.rules.transfers import rule_1a

ON = date(2026, 3, 2)
CONF = "ab12cd34ef"
# ── Rule 1a: Conf# hash match ──────────────────────────────────────────────


def test_1a_skips_a_same_day_same_amount_triple_as_ambiguous(add_txn, pool_for, run_rule):
    add_txn("checking", ON, "-200.00", f"Online transfer to CHK 2222 Conf# {CONF}")
    add_txn("savings", ON, "200.00", f"Online transfer from CHK 1111 Conf# {CONF}")
    add_txn("card", ON, "200.00", f"Online transfer from CHK 1111 Conf# {CONF}")
    pool = pool_for()

    assert run_rule(rule_1a, pool).drafts == []
    assert len(pool) == 3


# ── Rule 1b: Zelle confirmation number ─────────────────────────────────────


# ── Rule 2: card bill payment ──────────────────────────────────────────────


# ── Rule 2b: card credit balance refund ────────────────────────────────────


# ── Rule 3: generic transfer without a confirmation number ─────────────────
