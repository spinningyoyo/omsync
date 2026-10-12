"""Keyword matching, amount rules, explanation, and categorization previews and application."""

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

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
from omsync.ledger.models import AmountRule, Category, CategoryOrigin, Keyword, Transaction
from omsync.ledger.statements import source_texts
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
class ActiveAmountRule:
    """An enabled amount rule, as matching reads it."""

    id: int
    list_key: str
    from_category_id: int
    below_cents: int
    to_category_id: int


@dataclass(frozen=True)
class KeywordSet:
    """Everything that classifies a payee, loaded together so no caller can match keywords
    without the amount rules that adjust their result.

    lists holds each list's keywords in precedence order. amount_rules holds the rules with the
    lowest limit first, then the lowest id.
    """

    lists: Mapping[str, Sequence[CompiledKeyword]]
    amount_rules: Sequence[ActiveAmountRule]


def load_keywords(session: Session) -> KeywordSet:
    """Compile every enabled keyword, grouped by list, and read every enabled amount rule.

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
            AmountRule.id,
            AmountRule.list_key,
            AmountRule.from_category_id,
            AmountRule.below_cents,
            AmountRule.to_category_id,
        )
        .where(AmountRule.enabled.is_(True))
        .order_by(AmountRule.below_cents, AmountRule.id)
    ).all()
    return KeywordSet(
        {key: tuple(items) for key, items in lists.items()},
        tuple(ActiveAmountRule(*row) for row in rules),
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


def pick_amount_rule(
    keywords: KeywordSet, match: KeywordMatch, amount_cents: int
) -> ActiveAmountRule | None:
    """The amount rule that sends a keyword's category elsewhere, or None.

    A rule applies when it names the keyword's list and category and the absolute amount is
    strictly below its limit, so a refund behaves like its purchase. The lowest limit wins, and
    the lowest id among equals. The caller applies the one rule; rules never chain.
    """
    return next(
        (
            rule
            for rule in keywords.amount_rules
            if rule.list_key == match.list_key
            and rule.from_category_id == match.category_id
            and abs(amount_cents) < rule.below_cents
        ),
        None,
    )


@dataclass(frozen=True)
class Explanation:
    """How a payee classifies. matches are the keywords that hit, in precedence order, and the first
    is the winner. amount_rule is the rule that redirects the winner at the amount given, if any."""

    matches: list[KeywordMatch]
    amount_rule: ActiveAmountRule | None


def explain(session: Session, payee_text: str, amount_cents: int | None = None) -> Explanation:
    """Explain a payee's classification. Without an amount, no amount rule is considered."""
    keywords = load_keywords(session)
    matches = [_as_match(keyword) for keyword in _hits(keywords, PAYEE_LISTS, payee_text)]
    rule = None
    if matches and amount_cents is not None:
        rule = pick_amount_rule(keywords, matches[0], amount_cents)
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
    """How many transactions move from one category to another. None means unset."""

    from_name: str | None
    to_name: str | None
    count: int


@dataclass(frozen=True)
class CategorizePlan:
    """What categorize would do, computed without writing anything.

    preview is None when no transaction would change.
    """

    preview: Preview | None
    considered: int
    changed: int
    transitions: tuple[Transition, ...]


def plan_categorize(
    session: Session,
    *,
    account_ids: Iterable[int] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> CategorizePlan:
    """Plan keyword categories for the transactions in scope. Writes nothing.

    account_ids None means every account. start and end are inclusive dates. Only transactions
    without splits, and whose origin is none or keyword, are considered.
    """
    keywords = load_keywords(session)
    names: dict[int, str] = dict(session.execute(select(Category.id, Category.name)).all())
    query = (
        select(
            Transaction.id,
            Transaction.payee_raw,
            Transaction.payee,
            Transaction.amount_cents,
            Transaction.category_id,
            Transaction.category_origin,
        )
        .where(
            ~Transaction.splits.any(),
            Transaction.category_origin.in_([CategoryOrigin.none, CategoryOrigin.keyword]),
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
    moves: Counter[tuple[int | None, int | None]] = Counter()
    for row in rows:
        origin = CategoryOrigin(row.category_origin)
        match = match_payee(payee_match_text(row.payee_raw, row.payee), keywords)
        source = sources.get(row.id)
        if match is None and source is not None and (source.memo or "").strip():
            # The payee names no keyword, so the statement's MEMO may. Only Unique keywords apply
            # to memos: Generic ones are loose payee fragments that misfire on location text.
            # A payee that matched keeps its match, so a MEMO never overrides a payee decision.
            match = match_memo(source.memo or "", keywords)
        target = None
        if match is not None:
            rule = pick_amount_rule(keywords, match, row.amount_cents)
            target = match.category_id if rule is None else rule.to_category_id
        decision = _decide(row.category_id, origin, target)
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
        moves[(row.category_id, new_category)] += 1

    transitions = tuple(
        sorted(
            (
                Transition(_name(names, before), _name(names, after), count)
                for (before, after), count in moves.items()
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
    )


def _decide(
    current_category: int | None, origin: CategoryOrigin, target: int | None
) -> tuple[int | None, CategoryOrigin] | None:
    """The category and origin a transaction should have, or None to leave it alone.

    target is the category the winning keyword and its amount rule give, or None for no match.
    """
    if target is not None:
        if target == current_category:
            return None
        if not may_replace(CategoryOrigin.keyword, origin):
            return None
        return target, CategoryOrigin.keyword
    if origin is CategoryOrigin.keyword:
        # The keyword that set this category no longer matches, so it goes back to triage.
        return None, CategoryOrigin.none
    return None


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
