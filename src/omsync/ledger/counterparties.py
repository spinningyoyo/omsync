"""Counterparties: the people and businesses your P2P payments go to or come from.

A counterparty is identified by its payee text. payee_patterns is stored as {"out": [...],
"in": [...]}: "out" patterns match payments you sent to it, and "in" patterns match payments it
sent you. Each pattern is a case-insensitive regular expression, checked when you save it. A
counterparty with no_match_expected set is a standing acknowledgment: its payments are not
expected to have a counterpart. Writes go through omsync.core.batches, so each change can be
previewed and undone.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
    snapshot,
)
from omsync.ledger.links import Counterparty, CounterpartyKind

MAX_NAME_LENGTH = 80
MAX_PATTERNS_PER_SIDE = 10
MAX_PATTERN_LENGTH = 200


class NothingChanged(ValueError):
    """An update asked for the values already stored. Nothing was written."""


@dataclass(frozen=True)
class CounterpartyMatcher:
    """A counterparty with its patterns compiled, ready for matching payee text."""

    id: int
    name: str
    out_patterns: tuple[re.Pattern[str], ...]
    in_patterns: tuple[re.Pattern[str], ...]
    no_match_expected: bool = False

    def is_paid_to(self, payee: str) -> bool:
        """True when the payee text reads as a payment you sent to this counterparty."""
        return any(pattern.search(payee) for pattern in self.out_patterns)

    def is_paid_by(self, payee: str) -> bool:
        """True when the payee text reads as a payment this counterparty sent you."""
        return any(pattern.search(payee) for pattern in self.in_patterns)

    def matches(self, payee: str, amount_cents: int) -> bool:
        """True when the payee text matches the patterns on the side the amount's sign names."""
        return self.is_paid_to(payee) if amount_cents < 0 else self.is_paid_by(payee)


def validate_patterns(payee_patterns: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Check the {"out", "in"} shape and every pattern, and return the cleaned lists.

    A counterparty needs at least one pattern, on either side: one you only pay, or only receive
    from, leaves the other side empty. A pattern must compile, be at most 200 characters, and must
    not match the empty string, because that would match every payee.
    """
    if set(payee_patterns) != {"out", "in"}:
        raise ValueError('patterns need exactly two lists, "out" and "in"')
    cleaned: dict[str, list[str]] = {}
    for side in ("out", "in"):
        raw = payee_patterns[side]
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            raise ValueError(f'"{side}" must be a list of patterns')
        patterns = [str(item).strip() for item in raw]
        if len(patterns) > MAX_PATTERNS_PER_SIDE:
            raise ValueError(f"at most {MAX_PATTERNS_PER_SIDE} {side} patterns")
        for pattern in patterns:
            _check_pattern(pattern)
        cleaned[side] = patterns
    if not cleaned["out"] and not cleaned["in"]:
        raise ValueError("add at least one pattern")
    return cleaned


def create_counterparty(
    session: Session,
    *,
    name: str,
    kind: CounterpartyKind | str,
    payee_patterns: Mapping[str, Sequence[str]],
    idempotency_key: str,
    no_match_expected: bool = False,
) -> Batch:
    """Add a counterparty. Names are unique without regard to case."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    clean_name = _clean_name(session, name, except_id=None)
    values = {
        "id": next_primary_key(session, Counterparty.__table__),
        "name": clean_name,
        "kind": CounterpartyKind(kind).value,
        "payee_patterns": validate_patterns(payee_patterns),
        "no_match_expected": no_match_expected,
    }
    label = (
        f"No match expected from {clean_name}"
        if no_match_expected
        else f"Added counterparty {clean_name}"
    )
    return _write(
        session,
        "counterparty.create",
        [Change("counterparty", str(values["id"]), None, values)],
        label,
        idempotency_key,
    )


def update_counterparty(
    session: Session,
    counterparty_id: int,
    *,
    idempotency_key: str,
    name: str | None = None,
    kind: CounterpartyKind | str | None = None,
    payee_patterns: Mapping[str, Sequence[str]] | None = None,
    no_match_expected: bool | None = None,
) -> Batch:
    """Change a counterparty's name, kind, patterns, or standing acknowledgment. Raises
    LookupError if it does not exist, and NothingChanged when the values are already stored."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _snapshot_or_raise(session, counterparty_id)
    after: dict[str, Any] = dict(before)
    if name is not None:
        after["name"] = _clean_name(session, name, except_id=counterparty_id)
    if kind is not None:
        after["kind"] = CounterpartyKind(kind).value
    if payee_patterns is not None:
        after["payee_patterns"] = validate_patterns(payee_patterns)
    if no_match_expected is not None:
        after["no_match_expected"] = no_match_expected
    if after == before:
        raise NothingChanged("Nothing has changed.")
    return _write(
        session,
        "counterparty.update",
        [Change("counterparty", str(counterparty_id), before, after)],
        _update_label(before, after),
        idempotency_key,
    )


def _update_label(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Name the change: a lone switch of the standing acknowledgment says which way it went."""
    name = after["name"]
    others_same = all(before[key] == after[key] for key in before if key != "no_match_expected")
    if others_same and after["no_match_expected"] != before["no_match_expected"]:
        if after["no_match_expected"]:
            return f"No match expected from {name}"
        return f"Expecting matches from {name} again"
    return f"Updated counterparty {before['name']}"


def delete_counterparty(session: Session, counterparty_id: int, *, idempotency_key: str) -> Batch:
    """Remove a counterparty. Links that point at its payments are kept as they are."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _snapshot_or_raise(session, counterparty_id)
    return _write(
        session,
        "counterparty.delete",
        [Change("counterparty", str(counterparty_id), before, None)],
        f"Deleted counterparty {before['name']}",
        idempotency_key,
    )


def list_counterparties(session: Session) -> list[Counterparty]:
    """Return every counterparty, ordered by ID."""
    return list(session.scalars(select(Counterparty).order_by(Counterparty.id)))


def load_matchers(session: Session) -> list[CounterpartyMatcher]:
    """Compile every counterparty's patterns, ordered by ID, for the reconcile engine."""
    return [
        CounterpartyMatcher(
            id=row.id,
            name=row.name,
            out_patterns=_compile(row.payee_patterns["out"]),
            in_patterns=_compile(row.payee_patterns["in"]),
            no_match_expected=row.no_match_expected,
        )
        for row in list_counterparties(session)
    ]


def _compile(patterns: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


def _check_pattern(pattern: str) -> None:
    if not pattern:
        raise ValueError("a pattern cannot be empty")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"a pattern can be at most {MAX_PATTERN_LENGTH} characters")
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{pattern!r} is not a valid pattern: {exc}") from exc
    if compiled.search(""):
        raise ValueError(f"{pattern!r} would match every payee; make it more specific")


def _clean_name(session: Session, name: str, *, except_id: int | None) -> str:
    clean = " ".join(str(name).split())
    if not clean:
        raise ValueError("a counterparty needs a name")
    if len(clean) > MAX_NAME_LENGTH:
        raise ValueError(f"a name can be at most {MAX_NAME_LENGTH} characters")
    for row in session.scalars(select(Counterparty)):
        if row.id != except_id and row.name.casefold() == clean.casefold():
            raise ValueError(f"a counterparty named {row.name!r} already exists")
    return clean


def _snapshot_or_raise(session: Session, counterparty_id: int) -> dict[str, Any]:
    before = snapshot(session, "counterparty", counterparty_id)
    if before is None:
        raise LookupError(f"no counterparty {counterparty_id}")
    return before


def _write(
    session: Session, kind: str, changes: list[Change], label: str, idempotency_key: str
) -> Batch:
    preview = make_preview(session, kind, changes, summary={"label": label})
    return apply(session, preview, idempotency_key=idempotency_key)
