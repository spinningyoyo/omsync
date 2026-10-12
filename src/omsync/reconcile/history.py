"""What you have done with each rule's output: links in place, links you removed, proposals you
rejected. A rule whose links you keep removing stops being applied automatically.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.ledger.links import Link, LinkKind, RejectedProposal, RejectionReason

# The number of unlinked links after which a rule's automatic proposals wait for review.
DEMOTE_AFTER_UNLINKS = 2
# Rejections recorded before the rule was kept are shown under this name.
UNKNOWN_RULE = ""
# The rule recorded on a link made by hand.
MANUAL_RULE = "manual"


@dataclass(frozen=True)
class RuleTally:
    """One rule's record. paused is True when its automatic proposals now wait for review."""

    rule: str
    linked: int
    unlinked: int
    rejected: int

    @property
    def paused(self) -> bool:
        return self.unlinked >= DEMOTE_AFTER_UNLINKS


def unlink_counts(session: Session) -> dict[str, int]:
    """Return, for each rule, how many of its links you removed."""
    rows = session.execute(
        select(RejectedProposal.rule, func.count())
        .where(
            RejectedProposal.reason == RejectionReason.unlinked,
            RejectedProposal.rule.is_not(None),
        )
        .group_by(RejectedProposal.rule)
    )
    return {str(rule): int(count) for rule, count in rows}


def demoted_rules(session: Session) -> dict[str, int]:
    """Return the rules with enough unlinked links to lose automatic application, with the count."""
    return {
        rule: count
        for rule, count in unlink_counts(session).items()
        if count >= DEMOTE_AFTER_UNLINKS
    }


def rule_tally(session: Session) -> list[RuleTally]:
    """Return one tally per rule that has a link or a decision, ordered by rule key."""
    linked: dict[str, int] = {}
    for kind, evidence in session.execute(select(Link.kind, Link.evidence)):
        if kind is not LinkKind.receipt:
            rule = str((evidence or {}).get("rule") or MANUAL_RULE)
            linked[rule] = linked.get(rule, 0) + 1
    turned_down: dict[str, dict[RejectionReason, int]] = {}
    rows = session.execute(
        select(RejectedProposal.rule, RejectedProposal.reason, func.count()).group_by(
            RejectedProposal.rule, RejectedProposal.reason
        )
    )
    for rule, reason, count in rows:
        turned_down.setdefault(rule or UNKNOWN_RULE, {})[reason] = int(count)
    keys = sorted({*linked, *turned_down})
    return [
        RuleTally(
            rule=key,
            linked=linked.get(key, 0),
            unlinked=turned_down.get(key, {}).get(RejectionReason.unlinked, 0),
            rejected=turned_down.get(key, {}).get(RejectionReason.rejected, 0),
        )
        for key in keys
    ]
