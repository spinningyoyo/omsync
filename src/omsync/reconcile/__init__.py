"""Reconciliation: proposing the links that explain transactions, and recording your decisions.

model holds the values, pool the transactions a run may use, and rules/ the matching rules.
engine.run proposes without writing. decisions.py is the only place reconciliation writes, one
batch per decision.
"""
