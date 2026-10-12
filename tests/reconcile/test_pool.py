"""The pool: which transactions a run may use, and how rules drain it."""

from datetime import date

from omsync.reconcile.rules.base import Emitter
from omsync.reconcile.rules.transfers import rule_1a, rule_3

ON = date(2026, 3, 2)
CONF = "ab12cd34ef"


def test_a_transaction_claimed_by_an_earlier_rule_is_not_reused_by_a_later_one(add_txn, pool_for):
    add_txn("checking", ON, "-200.00", f"Online transfer to CHK 2222 Conf# {CONF}")
    add_txn("savings", ON, "200.00", f"Online transfer from CHK 1111 Conf# {CONF}")
    generic = add_txn("card2", ON, "200.00", "Credit Card Payment from 3333")
    pool = pool_for()

    emit = Emitter()
    rule_1a(pool, emit)
    rule_3(pool, emit)

    assert [draft.rule_key for draft in emit.drafts] == ["1a"]
    assert [txn.id for txn in pool] == [generic]
