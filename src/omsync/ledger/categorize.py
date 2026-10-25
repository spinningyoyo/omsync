"""Keyword matching, rules, explanation, and categorization previews and application."""

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    BatchError,
    Change,
    Preview,
    applied_batch_for_key,
    apply,
    make_preview,
)
from omsync.ledger.models import (
    Category,
    CategoryOrigin,
    CategoryRule,
    Contact,
    ContactRole,
    Keyword,
    RuleOtherSide,
    Transaction,
)
from omsync.ledger.other_side import PersonSides
from omsync.ledger.statements import SourceText, source_texts
from omsync.ledger.transactions import may_replace

PAYEE_LISTS = ("unique", "generic")
MEMO_LISTS = ("unique",)
_VENDOR_KEY = re.compile(r"[a-z][a-z0-9_]*")


class InvalidKeyword(ValueError):
    """A stored keyword cannot be compiled. The message names its id and list, not its pattern."""


def is_list_key(value: str) -> bool:
    """True for a payee list or a lowercase vendor key such as amazon."""
    return value in PAYEE_LISTS or _VENDOR_KEY.fullmatch(value) is not None


def compile_pattern(pattern: str, *, is_regex: bool) -> re.Pattern[str]:
    """Compile a keyword pattern, case-insensitively. A plain pattern matches only literally.

    Raises re.error if a regex does not compile.
    """
    return re.compile(pattern if is_regex else re.escape(pattern), re.IGNORECASE)


@dataclass(frozen=True)
class CompiledKeyword:
    id: int
    list_key: str
    pattern: str
    category_id: int
    priority: int
    matcher: re.Pattern[str]

    def matches(self, text: str) -> bool:
        return self.matcher.search(text) is not None


@dataclass(frozen=True)
class KeywordMatch:
    keyword_id: int
    category_id: int
    list_key: str
    pattern: str


@dataclass(frozen=True)
class ActiveRule:
    """An enabled rule, as matching reads it. position orders the rules; the first that holds wins.

    A draft that is not saved yet has id 0.
    """

    id: int
    position: int
    list_key: str | None
    from_category_id: int | None
    below_cents: int | None
    at_least_cents: int | None
    other_side: RuleOtherSide | None
    contact_id: int | None
    account_id: int | None
    to_category_id: int


@dataclass(frozen=True)
class KeywordSet:
    """Everything that classifies a payee, loaded together so no caller can match keywords
    without the rules that adjust their result.

    lists holds each list's keywords in precedence order. rules holds the enabled rules in
    position order.
    """

    lists: Mapping[str, Sequence[CompiledKeyword]]
    rules: Sequence[ActiveRule]


def load_keywords(session: Session) -> KeywordSet:
    """Compile every enabled keyword, grouped by list, and read every enabled rule.

    Precedence within a list is `_precedence`. Raises InvalidKeyword if a stored regex does not
    compile.
    """
    rows = session.execute(
        select(
            Keyword.id,
            Keyword.list_key,
            Keyword.pattern,
            Keyword.is_regex,
            Keyword.category_id,
            Keyword.priority,
        ).where(Keyword.enabled.is_(True))
    ).all()
    lists: dict[str, list[CompiledKeyword]] = {}
    for row in sorted(rows, key=lambda r: _precedence(r.priority, r.pattern, r.id)):
        try:
            matcher = compile_pattern(row.pattern, is_regex=row.is_regex)
        except re.error:
            raise InvalidKeyword(
                f"keyword {row.id} in list {row.list_key!r} is not a valid regular expression"
            ) from None
        lists.setdefault(row.list_key, []).append(
            CompiledKeyword(
                row.id, row.list_key, row.pattern, row.category_id, row.priority, matcher
            )
        )
    rules = session.execute(
        select(
            CategoryRule.id,
            CategoryRule.position,
            CategoryRule.list_key,
            CategoryRule.from_category_id,
            CategoryRule.below_cents,
            CategoryRule.at_least_cents,
            CategoryRule.other_side,
            CategoryRule.contact_id,
            CategoryRule.account_id,
            CategoryRule.to_category_id,
        )
        .where(CategoryRule.enabled.is_(True))
        .order_by(CategoryRule.position, CategoryRule.id)
    ).all()
    return KeywordSet(
        {key: tuple(items) for key, items in lists.items()},
        tuple(_active(row) for row in rules),
    )


def _active(row: Any) -> ActiveRule:
    """A stored rule's row as matching reads it."""
    return ActiveRule(
        id=row.id,
        position=row.position,
        list_key=row.list_key,
        from_category_id=row.from_category_id,
        below_cents=row.below_cents,
        at_least_cents=row.at_least_cents,
        other_side=None if row.other_side is None else RuleOtherSide(row.other_side),
        contact_id=row.contact_id,
        account_id=row.account_id,
        to_category_id=row.to_category_id,
    )


def _precedence(priority: int, pattern: str, keyword_id: int) -> tuple[int, int, int]:
    """Sort key for keywords of one list: higher priority first, then a longer pattern, then a
    lower id."""
    return (-priority, -len(pattern), keyword_id)


def payee_match_text(payee_raw: str, payee: str) -> str:
    """The text to match: the raw payee, or the payee when the raw payee is blank."""
    return payee_raw if payee_raw.strip() else payee


def match_payee(payee_text: str, keywords: KeywordSet) -> KeywordMatch | None:
    """The winning keyword for a payee: the first hit in unique, then in generic."""
    return _first(_hits(keywords, PAYEE_LISTS, payee_text))


def match_memo(memo_text: str, keywords: KeywordSet) -> KeywordMatch | None:
    """The winning unique keyword for a statement memo. Generic keywords are for payees only."""
    return _first(_hits(keywords, MEMO_LISTS, memo_text))


def match_receipt_item(vendor_key: str, text: str, keywords: KeywordSet) -> KeywordMatch | None:
    """The winning keyword for a receipt item, using only that vendor's list."""
    return _first(_hits(keywords, (vendor_key,), text))


def pick_rule(
    keywords: KeywordSet,
    match: KeywordMatch | None,
    *,
    amount_cents: int | None,
    contact_id: int | None = None,
    account_id: int | None = None,
    owner_id: int | None = None,
    with_person: bool = False,
) -> ActiveRule | None:
    """The first rule, in position order, whose conditions all hold, or None.

    match is the keyword that won the payee, or None when no keyword matched. A rule's keyword
    conditions read only that winner, so a rule with none can hold with no keyword. amount_cents
    is None when no amount is known, and then a rule with an amount condition does not hold.
    contact_id is the other side stored on the transaction, owner_id the contact that is you, and
    with_person whether the payment is with a person. The caller applies the one rule it returns;
    rules never chain.
    """
    return next(
        (
            rule
            for rule in keywords.rules
            if _holds(
                rule,
                match,
                amount_cents=amount_cents,
                contact_id=contact_id,
                account_id=account_id,
                owner_id=owner_id,
                with_person=with_person,
            )
        ),
        None,
    )


def _holds(
    rule: ActiveRule,
    match: KeywordMatch | None,
    *,
    amount_cents: int | None,
    contact_id: int | None,
    account_id: int | None,
    owner_id: int | None,
    with_person: bool,
) -> bool:
    if rule.from_category_id is not None:
        if match is None or match.category_id != rule.from_category_id:
            return False
        if rule.list_key is not None and match.list_key != rule.list_key:
            return False
    if rule.below_cents is not None or rule.at_least_cents is not None:
        if amount_cents is None:
            return False
        if rule.below_cents is not None and not abs(amount_cents) < rule.below_cents:
            return False
        if rule.at_least_cents is not None and not abs(amount_cents) >= rule.at_least_cents:
            return False
    if rule.other_side is RuleOtherSide.person and not with_person:
        return False
    if rule.other_side is RuleOtherSide.contact and contact_id != rule.contact_id:
        return False
    if rule.other_side is RuleOtherSide.owner_account and (
        owner_id is None or contact_id != owner_id
    ):
        return False
    return rule.account_id is None or account_id == rule.account_id


@dataclass(frozen=True)
class Explanation:
    """How a payee classifies. matches are the keywords that hit, in precedence order, and the first
    is the winner. rule is the first rule that holds, if any: it sets the category in place of the
    winner, and it can hold with no keyword match at all."""

    matches: list[KeywordMatch]
    rule: ActiveRule | None


def explain(
    session: Session,
    payee_text: str,
    amount_cents: int | None = None,
    *,
    with_person: bool = False,
) -> Explanation:
    """Explain a payee's classification from the payee alone. Without an amount, rules with an
    amount condition do not hold. A payee alone does not name a contact or an account, so rules that
    name one, or that name you as the other side, do not hold either. A rule limited to a person
    holds only when with_person says the payment is with a person."""
    keywords = load_keywords(session)
    matches = [_as_match(keyword) for keyword in _hits(keywords, PAYEE_LISTS, payee_text)]
    rule = pick_rule(
        keywords,
        matches[0] if matches else None,
        amount_cents=amount_cents,
        with_person=with_person,
    )
    return Explanation(matches, rule)


def _hits(keywords: KeywordSet, list_keys: Iterable[str], text: str) -> Iterator[CompiledKeyword]:
    for list_key in list_keys:
        for keyword in keywords.lists.get(list_key, ()):
            if keyword.matches(text):
                yield keyword


def _first(hits: Iterator[CompiledKeyword]) -> KeywordMatch | None:
    keyword = next(hits, None)
    return None if keyword is None else _as_match(keyword)


def _as_match(keyword: CompiledKeyword) -> KeywordMatch:
    return KeywordMatch(keyword.id, keyword.category_id, keyword.list_key, keyword.pattern)


# Categorize


@dataclass(frozen=True)
class Transition:
    """How many transactions move from one category to another, and the origin they take. None
    means unset. Between the same category it is an origin change alone: the category stays, and
    what set it changes, from a keyword to a rule, for instance."""

    from_name: str | None
    to_name: str | None
    count: int
    origin: CategoryOrigin


@dataclass(frozen=True)
class Move:
    """One transaction whose category categorize would change, as a preview lists it."""

    transaction_id: int
    posted_on: date
    payee: str
    amount_cents: int
    from_name: str | None
    to_name: str | None


@dataclass(frozen=True)
class CategorizePlan:
    """What categorize would do, computed without writing anything.

    preview is None when no transaction would change. moves lists each change in ID order.
    """

    preview: Preview | None
    considered: int
    changed: int
    transitions: tuple[Transition, ...]
    moves: tuple[Move, ...]


# The origins categorize may set. A keyword or a rule is automation that a rerun recomputes.
_AUTOMATIC = (CategoryOrigin.keyword, CategoryOrigin.rule)


def plan_categorize(
    session: Session,
    *,
    account_ids: Iterable[int] | None = None,
    start: date | None = None,
    end: date | None = None,
    rules: Sequence[ActiveRule] | None = None,
) -> CategorizePlan:
    """Plan categories for the transactions in scope. Writes nothing.

    account_ids None means every account. start and end are inclusive dates. Only transactions
    without splits, and whose origin is none, keyword, or rule, are considered: a manual or receipt
    category is never changed. rules, in position order, replaces the stored rules, which is how a
    draft is previewed; None reads the stored rules. A rule that reads the other side or the
    account reads it as stored on each transaction, so recognize the other side first.
    """
    keywords = load_keywords(session)
    if rules is not None:
        keywords = KeywordSet(keywords.lists, tuple(rules))
    # Who a payment is with is read only when a person rule could use it.
    people = (
        PersonSides(session)
        if any(rule.other_side is RuleOtherSide.person for rule in keywords.rules)
        else None
    )
    owner_id = session.scalar(select(Contact.id).where(Contact.role == ContactRole.owner))
    names: dict[int, str] = dict(session.execute(select(Category.id, Category.name)).all())
    query = (
        select(
            Transaction.id,
            Transaction.account_id,
            Transaction.posted_on,
            Transaction.payee_raw,
            Transaction.payee,
            Transaction.amount_cents,
            Transaction.contact_id,
            Transaction.category_id,
            Transaction.category_origin,
        )
        .where(
            ~Transaction.splits.any(),
            Transaction.category_origin.in_(
                [CategoryOrigin.none, CategoryOrigin.keyword, CategoryOrigin.rule]
            ),
        )
        .order_by(Transaction.id)
    )
    if account_ids is not None:
        query = query.where(Transaction.account_id.in_(list(account_ids)))
    if start is not None:
        query = query.where(Transaction.posted_on >= start)
    if end is not None:
        query = query.where(Transaction.posted_on <= end)

    rows = session.execute(query).all()
    sources = source_texts(session, [row.id for row in rows])
    changes: list[Change] = []
    moves: list[Move] = []
    counts: Counter[tuple[int | None, int | None, CategoryOrigin]] = Counter()
    for row in rows:
        origin = CategoryOrigin(row.category_origin)
        match, rule = _classify(keywords, people, owner_id, row, sources.get(row.id))
        decision = _decide(row.category_id, origin, _target(match, rule))
        if decision is None:
            continue
        new_category, new_origin = decision
        changes.append(
            Change(
                "transaction",
                str(row.id),
                {"category_id": row.category_id, "category_origin": origin.value},
                {"category_id": new_category, "category_origin": new_origin.value},
            )
        )
        counts[(row.category_id, new_category, new_origin)] += 1
        moves.append(
            Move(
                transaction_id=row.id,
                posted_on=row.posted_on,
                payee=row.payee,
                amount_cents=row.amount_cents,
                from_name=_name(names, row.category_id),
                to_name=_name(names, new_category),
            )
        )

    transitions = tuple(
        sorted(
            (
                Transition(_name(names, before), _name(names, after), count, origin)
                for (before, after, origin), count in counts.items()
            ),
            key=lambda t: (-t.count, t.from_name or "", t.to_name or ""),
        )
    )
    preview = (
        make_preview(
            session,
            "categorize",
            changes,
            summary={"considered": len(rows), "changed": len(changes)},
        )
        if changes
        else None
    )
    return CategorizePlan(
        preview=preview,
        considered=len(rows),
        changed=len(changes),
        transitions=transitions,
        moves=tuple(moves),
    )


def rule_for_transaction(session: Session, transaction_id: int) -> ActiveRule | None:
    """The enabled rule that holds for one transaction now, read as categorize reads it, or None.

    The other side is read as it is stored, as categorize on demand reads it. Writes nothing.
    """
    row = session.execute(
        select(
            Transaction.payee_raw,
            Transaction.payee,
            Transaction.amount_cents,
            Transaction.contact_id,
            Transaction.account_id,
        ).where(Transaction.id == transaction_id)
    ).one_or_none()
    if row is None:
        return None
    keywords = load_keywords(session)
    people = (
        PersonSides(session)
        if any(rule.other_side is RuleOtherSide.person for rule in keywords.rules)
        else None
    )
    owner_id = session.scalar(select(Contact.id).where(Contact.role == ContactRole.owner))
    source = source_texts(session, [transaction_id]).get(transaction_id)
    return _classify(keywords, people, owner_id, row, source)[1]


def _classify(
    keywords: KeywordSet,
    people: PersonSides | None,
    owner_id: int | None,
    row: Any,
    source: SourceText | None,
) -> tuple[KeywordMatch | None, ActiveRule | None]:
    """The keyword that wins a transaction and the rule that holds for it, as categorize reads them.

    row carries the payee, payee_raw, amount_cents, contact_id, and account_id of a transaction.
    people is None when no rule reads a person, so no person is looked up.
    """
    match = match_payee(payee_match_text(row.payee_raw, row.payee), keywords)
    if match is None and source is not None and (source.memo or "").strip():
        # The payee names no keyword, so the statement's MEMO may. Only Unique keywords apply
        # to memos: Generic ones are loose payee fragments that misfire on location text.
        # A payee that matched keeps its match, so a MEMO never overrides a payee decision.
        match = match_memo(source.memo or "", keywords)
    with_person = people is not None and people.with_person(
        contact_id=row.contact_id,
        payee_raw=row.payee_raw,
        source=source,
        amount_cents=row.amount_cents,
    )
    rule = pick_rule(
        keywords,
        match,
        amount_cents=row.amount_cents,
        contact_id=row.contact_id,
        account_id=row.account_id,
        owner_id=owner_id,
        with_person=with_person,
    )
    return match, rule


def _target(
    match: KeywordMatch | None, rule: ActiveRule | None
) -> tuple[int | None, CategoryOrigin]:
    """The category and origin that the keyword and rule give a transaction. A rule that holds
    sets its category in place of the keyword's; with neither, the category is unset."""
    if rule is not None:
        return rule.to_category_id, CategoryOrigin.rule
    if match is not None:
        return match.category_id, CategoryOrigin.keyword
    return None, CategoryOrigin.none


def _decide(
    current_category: int | None,
    origin: CategoryOrigin,
    target: tuple[int | None, CategoryOrigin],
) -> tuple[int | None, CategoryOrigin] | None:
    """The category and origin a transaction should have, or None to leave it alone.

    target is what the keyword and rules give, from `_target`. A category set by you or by a
    receipt is never reached here, because plan_categorize does not read those transactions.
    """
    category, new_origin = target
    if category is None:
        if origin in _AUTOMATIC:
            # What set this category no longer holds, so it goes back to triage.
            return None, CategoryOrigin.none
        return None
    if (category, new_origin) == (current_category, origin):
        return None
    if not may_replace(new_origin, origin):
        return None
    return category, new_origin


def _name(names: dict[int, str], category_id: int | None) -> str | None:
    return None if category_id is None else names.get(category_id)


def apply_categorize(session: Session, plan: CategorizePlan, *, idempotency_key: str) -> Batch:
    """Write a categorize plan as one batch. The same key applies at most once.

    Raises BatchError if the plan has no changes, and StalePreview if the data changed since
    the plan was made.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if plan.preview is None:
        raise BatchError("nothing to categorize: no transaction would change")
    return apply(session, plan.preview, idempotency_key=idempotency_key)


# Conflicts


class ConflictScope(StrEnum):
    same_list = "same_list"  # one pattern, several categories, inside one list
    across_lists = "across_lists"  # unique and generic give one pattern different categories


@dataclass(frozen=True)
class ConflictMember:
    keyword_id: int
    list_key: str
    pattern: str
    category_name: str


@dataclass(frozen=True)
class Conflict:
    pattern: str  # as first written
    scope: ConflictScope
    members: tuple[ConflictMember, ...]
    winner_id: int  # the member whose category a payee hitting all of them gets


def find_conflicts(session: Session) -> list[Conflict]:
    """Find enabled keywords that share a pattern, ignoring case, but name different categories.

    A conflict is either inside one list, or between unique and generic. Repeating a pattern
    with the same category is not a conflict. The winner follows matching's own order: unique
    before generic, then precedence within the list.
    """
    rows = session.execute(
        select(Keyword.id, Keyword.list_key, Keyword.pattern, Keyword.priority, Category.name)
        .join(Category, Category.id == Keyword.category_id)
        .where(Keyword.enabled.is_(True))
        .order_by(Keyword.id)
    ).all()
    members = [ConflictMember(row.id, row.list_key, row.pattern, row.name) for row in rows]
    precedence = {row.id: _precedence(row.priority, row.pattern, row.id) for row in rows}

    def winner_id(group: Sequence[ConflictMember]) -> int:
        def rank(member: ConflictMember) -> tuple[int, tuple[int, int, int]]:
            list_rank = PAYEE_LISTS.index(member.list_key) if member.list_key in PAYEE_LISTS else 0
            return list_rank, precedence[member.keyword_id]

        return min(group, key=rank).keyword_id

    by_list: dict[tuple[str, str], list[ConflictMember]] = {}
    by_payee_pattern: dict[str, list[ConflictMember]] = {}
    for member in members:
        folded = member.pattern.casefold()
        by_list.setdefault((member.list_key, folded), []).append(member)
        if member.list_key in PAYEE_LISTS:
            by_payee_pattern.setdefault(folded, []).append(member)

    conflicts = [
        Conflict(group[0].pattern, ConflictScope.same_list, tuple(group), winner_id(group))
        for group in by_list.values()
        if len({member.category_name for member in group}) > 1
    ]
    for group in by_payee_pattern.values():
        unique = {m.category_name for m in group if m.list_key == "unique"}
        generic = {m.category_name for m in group if m.list_key == "generic"}
        if unique and generic and len(unique | generic) > 1:
            conflicts.append(
                Conflict(
                    group[0].pattern, ConflictScope.across_lists, tuple(group), winner_id(group)
                )
            )
    return conflicts
