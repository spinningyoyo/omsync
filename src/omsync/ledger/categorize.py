"""Keyword matching, rules, explanation, and categorization previews and application."""

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
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
from omsync.ledger.counterparty import PersonCounterparties
from omsync.ledger.models import (
    Category,
    CategoryOrigin,
    CategoryRule,
    Contact,
    ContactRole,
    Keyword,
    RuleCounterparty,
    Transaction,
)
from omsync.ledger.payment_methods import PaymentMethod, parse_paid_by, payment_method
from omsync.ledger.statements import SourceText, source_texts
from omsync.ledger.transactions import may_replace

PAYEE_LIST = "payee"
_VENDOR_KEY = re.compile(r"[a-z][a-z0-9_]*")


class InvalidKeyword(ValueError):
    """A stored keyword cannot be compiled. The message names its id and list, not its pattern."""


def is_list_key(value: str) -> bool:
    """True for the payee list or a lowercase vendor key such as amazon."""
    return value == PAYEE_LIST or _VENDOR_KEY.fullmatch(value) is not None


def list_name(list_key: str) -> str:
    """A list as the page names it: Payees, or a vendor's key capitalized, such as Amazon."""
    return "Payees" if list_key == PAYEE_LIST else list_key.capitalize()


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
    """An enabled rule, as matching reads it. Rules categorize only what no keyword matched;
    position orders them, and the first that holds wins.

    paid_by is empty when the rule does not read how money moved. A draft that is not saved yet
    has id 0.
    """

    id: int
    position: int
    paid_by: frozenset[PaymentMethod]
    below_cents: int | None
    at_least_cents: int | None
    counterparty: RuleCounterparty | None
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
        ).where(Keyword.enabled.is_(True))
    ).all()
    lists: dict[str, list[CompiledKeyword]] = {}
    for row in sorted(rows, key=lambda r: _precedence(r.pattern, r.id)):
        try:
            matcher = compile_pattern(row.pattern, is_regex=row.is_regex)
        except re.error:
            raise InvalidKeyword(
                f"keyword {row.id} in list {row.list_key!r} is not a valid regular expression"
            ) from None
        lists.setdefault(row.list_key, []).append(
            CompiledKeyword(row.id, row.list_key, row.pattern, row.category_id, matcher)
        )
    rules = session.execute(
        select(
            CategoryRule.id,
            CategoryRule.position,
            CategoryRule.paid_by,
            CategoryRule.below_cents,
            CategoryRule.at_least_cents,
            CategoryRule.counterparty,
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
        paid_by=parse_paid_by(row.paid_by),
        below_cents=row.below_cents,
        at_least_cents=row.at_least_cents,
        counterparty=None if row.counterparty is None else RuleCounterparty(row.counterparty),
        contact_id=row.contact_id,
        account_id=row.account_id,
        to_category_id=row.to_category_id,
    )


def with_keyword(
    keywords: KeywordSet, keyword: CompiledKeyword | None, *, replacing: int | None = None
) -> KeywordSet:
    """The set with a draft keyword in its list, in precedence order, which is how a draft is
    previewed. replacing removes the stored keyword of that id first, so an edit replaces it; a
    draft of None leaves it out, as for a keyword that is switched off."""
    lists = {
        key: [item for item in items if item.id != replacing]
        for key, items in keywords.lists.items()
    }
    if keyword is not None:
        members = [*lists.get(keyword.list_key, []), keyword]
        lists[keyword.list_key] = sorted(
            members, key=lambda item: _precedence(item.pattern, item.id)
        )
    return KeywordSet({key: tuple(items) for key, items in lists.items()}, keywords.rules)


def _precedence(pattern: str, keyword_id: int) -> tuple[int, int]:
    """Sort key for keywords of one list: the longer, more specific pattern first, then the older
    keyword."""
    return (-len(pattern), keyword_id)


def payee_match_text(payee_raw: str, payee: str) -> str:
    """The text to match: the raw payee, or the payee when the raw payee is blank."""
    return payee_raw if payee_raw.strip() else payee


def match_payee(payee_text: str, keywords: KeywordSet) -> KeywordMatch | None:
    """The winning payee keyword for a payee, or None."""
    return _first(_hits(keywords, (PAYEE_LIST,), payee_text))


def match_memo(memo_text: str, keywords: KeywordSet) -> KeywordMatch | None:
    """The winning payee keyword for a statement memo, or None."""
    return _first(_hits(keywords, (PAYEE_LIST,), memo_text))


def match_receipt_item(vendor_key: str, text: str, keywords: KeywordSet) -> KeywordMatch | None:
    """The winning keyword for a receipt item, using only that vendor's list."""
    return _first(_hits(keywords, (vendor_key,), text))


def pick_rule(
    keywords: KeywordSet,
    *,
    amount_cents: int | None,
    method: PaymentMethod | None = None,
    contact_id: int | None = None,
    account_id: int | None = None,
    owner_id: int | None = None,
    with_person: bool = False,
) -> ActiveRule | None:
    """The first rule, in position order, whose conditions all hold, or None.

    Call it only for a transaction no keyword matched: a keyword always beats a rule, so what was
    bought beats how it was paid. method is how the bank's wording says the money moved, or None
    when it does not say. amount_cents is None when no amount is known, and then a rule with an
    amount condition does not hold.
    contact_id is the counterparty stored on the transaction, owner_id the contact that is you, and
    with_person whether the payment is with a person. The caller applies the one rule it returns;
    rules never chain.
    """
    return next(
        (
            rule
            for rule in keywords.rules
            if _holds(
                rule,
                amount_cents=amount_cents,
                method=method,
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
    *,
    amount_cents: int | None,
    method: PaymentMethod | None,
    contact_id: int | None,
    account_id: int | None,
    owner_id: int | None,
    with_person: bool,
) -> bool:
    if rule.paid_by and method not in rule.paid_by:
        return False
    if rule.below_cents is not None or rule.at_least_cents is not None:
        if amount_cents is None:
            return False
        if rule.below_cents is not None and not abs(amount_cents) < rule.below_cents:
            return False
        if rule.at_least_cents is not None and not abs(amount_cents) >= rule.at_least_cents:
            return False
    if rule.counterparty is RuleCounterparty.person and not with_person:
        return False
    if rule.counterparty is RuleCounterparty.contact and contact_id != rule.contact_id:
        return False
    if rule.counterparty is RuleCounterparty.owner_account and (
        owner_id is None or contact_id != owner_id
    ):
        return False
    return rule.account_id is None or account_id == rule.account_id


@dataclass(frozen=True)
class Explanation:
    """How a payee classifies. matches are the keywords that hit, in precedence order, and the first
    is the winner. method and rule are read only when no keyword matched: method is how the
    wording says the money moved, and rule is the first rule that holds, if any."""

    matches: list[KeywordMatch]
    method: PaymentMethod | None
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
    name one, or that name you as the counterparty, do not hold either. A rule limited to a person
    holds only when with_person says the payment is with a person."""
    keywords = load_keywords(session)
    matches = [_as_match(keyword) for keyword in _hits(keywords, (PAYEE_LIST,), payee_text)]
    if matches:
        return Explanation(matches, None, None)
    method = payment_method(payee_text)
    rule = pick_rule(keywords, amount_cents=amount_cents, method=method, with_person=with_person)
    return Explanation(matches, method, rule)


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
    keywords: KeywordSet | None = None,
) -> CategorizePlan:
    """Plan categories for the transactions in scope. Writes nothing.

    account_ids None means every account. start and end are inclusive dates. Only transactions
    without splits, and whose origin is none, keyword, or rule, are considered: a manual or receipt
    category is never changed. rules, in position order, replaces the stored rules, which is how a
    draft is previewed; None reads the stored rules. keywords, likewise, replaces the stored
    keywords and rules (see with_keyword); rules, when also given, replaces its rules. A rule that
    reads the counterparty or the account reads it as stored on each transaction, so recognize the
    counterparty first.
    """
    keywords = keywords if keywords is not None else load_keywords(session)
    if rules is not None:
        keywords = KeywordSet(keywords.lists, tuple(rules))
    # Who a payment is with is read only when a person rule could use it.
    people = (
        PersonCounterparties(session)
        if any(rule.counterparty is RuleCounterparty.person for rule in keywords.rules)
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

    The counterparty is read as it is stored, as categorize on demand reads it. Writes nothing.
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
        PersonCounterparties(session)
        if any(rule.counterparty is RuleCounterparty.person for rule in keywords.rules)
        else None
    )
    owner_id = session.scalar(select(Contact.id).where(Contact.role == ContactRole.owner))
    source = source_texts(session, [transaction_id]).get(transaction_id)
    return _classify(keywords, people, owner_id, row, source)[1]


def _classify(
    keywords: KeywordSet,
    people: PersonCounterparties | None,
    owner_id: int | None,
    row: Any,
    source: SourceText | None,
) -> tuple[KeywordMatch | None, ActiveRule | None]:
    """The keyword that wins a transaction, or, when none does, the rule that holds for it.

    row carries the payee, payee_raw, amount_cents, contact_id, and account_id of a transaction.
    people is None when no rule reads a person, so no person is looked up.
    """
    text = payee_match_text(row.payee_raw, row.payee)
    match = match_payee(text, keywords)
    if match is None and source is not None and (source.memo or "").strip():
        # The payee names no keyword, so the statement's MEMO may. A payee that matched keeps
        # its match, so a MEMO never overrides a payee decision.
        match = match_memo(source.memo or "", keywords)
    if match is not None:
        # A keyword says who was paid, and that beats every rule.
        return match, None
    with_person = people is not None and people.with_person(
        contact_id=row.contact_id,
        payee_raw=row.payee_raw,
        source=source,
        amount_cents=row.amount_cents,
    )
    rule = pick_rule(
        keywords,
        amount_cents=row.amount_cents,
        method=payment_method(text),
        contact_id=row.contact_id,
        account_id=row.account_id,
        owner_id=owner_id,
        with_person=with_person,
    )
    return match, rule


def _target(
    match: KeywordMatch | None, rule: ActiveRule | None
) -> tuple[int | None, CategoryOrigin]:
    """The category and origin that the keyword or rule gives a transaction; with neither, the
    category is unset. A rule is only found when no keyword matched."""
    if match is not None:
        return match.category_id, CategoryOrigin.keyword
    if rule is not None:
        return rule.to_category_id, CategoryOrigin.rule
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


@dataclass(frozen=True)
class ConflictMember:
    keyword_id: int
    list_key: str
    pattern: str
    category_name: str


@dataclass(frozen=True)
class Conflict:
    pattern: str  # as first written
    members: tuple[ConflictMember, ...]
    winner_id: int  # the member whose category a payee hitting all of them gets


def find_conflicts(session: Session) -> list[Conflict]:
    """Find enabled keywords of one list that share a pattern, ignoring case, but name different
    categories. Repeating a pattern with the same category is not a conflict. The winner follows
    matching's own precedence."""
    rows = session.execute(
        select(Keyword.id, Keyword.list_key, Keyword.pattern, Category.name)
        .join(Category, Category.id == Keyword.category_id)
        .where(Keyword.enabled.is_(True))
        .order_by(Keyword.id)
    ).all()
    precedence = {row.id: _precedence(row.pattern, row.id) for row in rows}
    groups: dict[tuple[str, str], list[ConflictMember]] = {}
    for row in rows:
        member = ConflictMember(row.id, row.list_key, row.pattern, row.name)
        groups.setdefault((row.list_key, row.pattern.casefold()), []).append(member)
    return [
        Conflict(
            group[0].pattern,
            tuple(group),
            min(group, key=lambda member: precedence[member.keyword_id]).keyword_id,
        )
        for group in groups.values()
        if len({member.category_name for member in group}) > 1
    ]
