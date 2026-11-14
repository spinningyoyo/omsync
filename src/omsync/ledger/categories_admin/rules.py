"""Rule writes, and the previews of a rule and a keyword."""

from collections.abc import Mapping
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    next_primary_key,
)
from omsync.ledger.categories_admin.helpers import (
    _check_category,
    _check_list_key,
    _check_rule,
    _clean_pattern,
    _ensure_pattern_free,
    _ensure_rule_free,
    _rule_snapshot,
    _write,
)
from omsync.ledger.categories_admin.reads import _fields_of, _sentence_for
from omsync.ledger.categories_admin.rows import (
    NOTHING_CHANGED,
    DraftPreview,
    NothingChanged,
    RuleDraft,
)
from omsync.ledger.categorize import (
    ActiveRule,
    CompiledKeyword,
    compile_pattern,
    load_keywords,
    plan_categorize,
    with_keyword,
)
from omsync.ledger.models import (
    CategoryRule,
    Keyword,
    RuleCounterparty,
)
from omsync.ledger.payment_methods import (
    parse_paid_by,
)


def add_rule(session: Session, draft: RuleDraft, *, idempotency_key: str) -> Batch:
    """Add a rule at the end of the order, so it is tried after every rule already there. Raises
    ValueError with a message for the screen when the draft is refused, and nothing is written."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    values = _check_rule(session, draft)
    _ensure_rule_free(session, values, except_id=None)
    new_id = next_primary_key(session, CategoryRule.__table__)
    last = session.scalar(select(func.max(CategoryRule.position)))
    row = {"id": new_id, "position": (last or 0) + 1, "enabled": True, **values}
    return _write(
        session,
        "rule.add",
        [Change("category_rule", str(new_id), None, row)],
        f"Added rule: {_sentence_for(session, values)}",
        idempotency_key,
        rule_id=new_id,
    )


def update_rule(session: Session, rule_id: int, draft: RuleDraft, *, idempotency_key: str) -> Batch:
    """Replace a rule's conditions and category. Its place in the order and whether it is on stay as
    they are. Raises LookupError if it does not exist, ValueError for a refused draft, and
    NothingChanged when the values are already stored."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _rule_snapshot(session, rule_id)
    values = _check_rule(session, draft)
    after = {**before, **values}
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    _ensure_rule_free(session, values, except_id=rule_id)
    return _write(
        session,
        "rule.update",
        [Change("category_rule", str(rule_id), before, after)],
        f"Updated rule: {_sentence_for(session, values)}",
        idempotency_key,
        rule_id=rule_id,
    )


def set_rule_enabled(
    session: Session, rule_id: int, enabled: bool, *, idempotency_key: str
) -> Batch:
    """Turn a rule on or off. A disabled rule is skipped by categorize."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _rule_snapshot(session, rule_id)
    after = {**before, "enabled": bool(enabled)}
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    verb = "Turned on" if enabled else "Turned off"
    return _write(
        session,
        "rule.enable" if enabled else "rule.disable",
        [Change("category_rule", str(rule_id), before, after)],
        f"{verb} rule: {_sentence_for(session, before)}",
        idempotency_key,
        rule_id=rule_id,
    )


def delete_rule(session: Session, rule_id: int, *, idempotency_key: str) -> Batch:
    """Delete a rule. Undo restores it with its id. Transactions it set keep its origin until the
    next categorize, which sets them again or leaves them unset."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _rule_snapshot(session, rule_id)
    return _write(
        session,
        "rule.delete",
        [Change("category_rule", str(rule_id), before, None)],
        f"Deleted rule: {_sentence_for(session, before)}",
        idempotency_key,
        rule_id=rule_id,
    )


def move_rule(
    session: Session,
    rule_id: int,
    direction: Literal["up", "down"],
    *,
    idempotency_key: str,
) -> Batch:
    """Move a rule one place earlier (up) or later (down), by swapping places with the rule beside
    it, enabled or not, as one batch. Raises LookupError if the rule does not exist, ValueError for
    a direction other than up or down, and NothingChanged at the first or last place."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if direction not in ("up", "down"):
        raise ValueError("Move a rule up or down.")
    ordered = list(
        session.scalars(select(CategoryRule.id).order_by(CategoryRule.position, CategoryRule.id))
    )
    if rule_id not in ordered:
        raise LookupError(f"There is no rule {rule_id}.")
    index = ordered.index(rule_id)
    other_index = index - 1 if direction == "up" else index + 1
    if not 0 <= other_index < len(ordered):
        raise NothingChanged("It is already first." if direction == "up" else "It is already last.")
    mover = _rule_snapshot(session, rule_id)
    other = _rule_snapshot(session, ordered[other_index])
    if mover["position"] == other["position"]:
        raise NothingChanged(NOTHING_CHANGED)
    # Two rows change, each once, so undo can restore each from its own snapshot.
    changes = [
        Change("category_rule", str(rule_id), mover, {**mover, "position": other["position"]}),
        Change("category_rule", str(other["id"]), other, {**other, "position": mover["position"]}),
    ]
    return _write(
        session,
        "rule.move",
        changes,
        f"Moved rule {direction}: {_sentence_for(session, mover)}",
        idempotency_key,
        rule_id=rule_id,
    )


def preview_rule(session: Session, draft: RuleDraft, *, rule_id: int | None = None) -> DraftPreview:
    """The transactions whose category would change if the draft were saved now, with their count.

    A draft with rule_id replaces that rule, keeping its place and whether it is on. Without
    rule_id, the draft would be added last. Writes nothing. Raises ValueError for a draft that
    add_rule or update_rule would refuse, and LookupError if rule_id does not exist.
    """
    values = _check_rule(session, draft)
    stored = list(
        session.scalars(select(CategoryRule).order_by(CategoryRule.position, CategoryRule.id))
    )
    if rule_id is not None and all(rule.id != rule_id for rule in stored):
        raise LookupError(f"There is no rule {rule_id}.")
    rules: list[ActiveRule] = []
    for rule in stored:
        if rule.id == rule_id:
            if rule.enabled:
                rules.append(_active(rule.id, rule.position, values))
        elif rule.enabled:
            rules.append(_active(rule.id, rule.position, _fields_of(rule)))
    if rule_id is None:
        rules.append(_active(0, (stored[-1].position + 1) if stored else 1, values))
    plan = plan_categorize(session, rules=rules)
    return DraftPreview(count=plan.changed, changes=plan.moves)


def preview_keyword(
    session: Session,
    list_key: str,
    pattern: str,
    category_id: int,
    *,
    is_regex: bool = True,
    keyword_id: int | None = None,
) -> DraftPreview:
    """The transactions whose category would change if the keyword were saved now, with their count.

    A draft with keyword_id replaces that keyword, which stays out if it is switched off. Without
    keyword_id, the draft would be added. Writes nothing. Raises ValueError for a draft that
    add_keyword or update_keyword would refuse, and LookupError if keyword_id does not exist. A
    receipt list's keyword changes no transaction here: it categorizes receipt items, which change
    when a vendor's receipts are applied.
    """
    _check_list_key(list_key)
    clean = _clean_pattern(pattern, is_regex=is_regex)
    _check_category(session, category_id)
    enabled = True
    if keyword_id is not None:
        stored = session.get(Keyword, keyword_id)
        if stored is None:
            raise LookupError(f"There is no keyword {keyword_id}.")
        list_key, enabled = stored.list_key, stored.enabled
    _ensure_pattern_free(session, list_key, clean, except_id=keyword_id)
    if keyword_id is None:
        keyword_id_for_draft = next_primary_key(session, Keyword.__table__)
    else:
        keyword_id_for_draft = keyword_id
    draft = CompiledKeyword(
        keyword_id_for_draft,
        list_key,
        clean,
        category_id,
        compile_pattern(clean, is_regex=is_regex),
    )
    keywords = with_keyword(
        load_keywords(session), draft if enabled else None, replacing=keyword_id
    )
    plan = plan_categorize(session, keywords=keywords)
    return DraftPreview(count=plan.changed, changes=plan.moves)


def _active(rule_id: int, position: int, fields: Mapping[str, Any]) -> ActiveRule:
    other = fields["counterparty"]
    return ActiveRule(
        id=rule_id,
        position=position,
        paid_by=parse_paid_by(fields["paid_by"]),
        below_cents=fields["below_cents"],
        at_least_cents=fields["at_least_cents"],
        counterparty=None if other is None else RuleCounterparty(other),
        contact_id=fields["contact_id"],
        account_id=fields["account_id"],
        to_category_id=fields["to_category_id"],
    )
