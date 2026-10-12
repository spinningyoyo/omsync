"""Mapping statement account IDs to accounts, and creating accounts and aliases as batches.

An account is identified by its row, not by its statement account ID. Several statement
account IDs can map to one account through AccountAlias, as when a card is reissued.

Statement account IDs are sensitive. Messages here show only the last four characters, and
for an ID of four characters or fewer, nothing at all.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
)
from omsync.ledger.models import (
    Account,
    AccountAlias,
    AccountType,
    Institution,
    Provider,
    ProviderAccount,
)

_MASK = "****"
_LAST4 = re.compile(r"[0-9]{4}")
NAME_MAX = 60


class UnmappedAccount(Exception):
    """No account is mapped to this statement account ID. The message gives only its last four."""

    def __init__(self, institution_key: str, statement_account_last4: str) -> None:
        self.institution_key = institution_key
        self.statement_account_last4 = statement_account_last4
        super().__init__(
            f"no account at {institution_key} is mapped to a statement account "
            f"ending in {statement_account_last4}"
        )


class AccountError(Exception):
    """An account or alias cannot be created as asked. Nothing was written."""


def last4_of(statement_account_id: str) -> str:
    """Return the last four characters of a statement account ID, for display.

    An ID of exactly four characters is already just a last-four label, such as a CSV
    account folder named "1234", so it is shown. Shorter IDs are masked.
    """
    if len(statement_account_id) < 4:
        return _MASK
    return statement_account_id[-4:]


def find_account(
    session: Session, institution_key: str, statement_account_id: str
) -> Account | None:
    """Return the account a statement account ID maps to at an institution, or None."""
    return session.scalars(
        select(Account)
        .join(AccountAlias, AccountAlias.account_id == Account.id)
        .join(Institution, Institution.id == AccountAlias.institution_id)
        .where(
            Institution.key == institution_key,
            Account.institution_id == AccountAlias.institution_id,
            AccountAlias.statement_account_id == statement_account_id,
        )
    ).first()


def create_account(
    session: Session,
    *,
    institution_key: str,
    institution_name: str,
    statement_account_id: str,
    name: str,
    account_type: AccountType | str,
    last4: str | None,
    joint: bool = False,
    idempotency_key: str,
) -> Batch:
    """Create an account, its alias, and the institution if it is missing, as one batch. When the
    institution has exactly one provider, the account joins it, so that provider's next sync fetches
    it. With several providers the account is left for the person to assign.

    Raises AccountError if the statement account ID is already mapped. Raises ValueError if
    account_type is not an account type.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    type_value = AccountType(account_type).value
    if find_account(session, institution_key, statement_account_id) is not None:
        raise AccountError(
            f"a statement account ending in {last4_of(statement_account_id)} "
            "is already mapped to an account"
        )

    changes: list[Change] = []
    institution = session.scalars(
        select(Institution).where(Institution.key == institution_key)
    ).first()
    if institution is None:
        institution_id = next_primary_key(session, Institution.__table__)
        changes.append(
            Change(
                "institution",
                str(institution_id),
                None,
                {"id": institution_id, "key": institution_key, "name": institution_name},
            )
        )
    else:
        institution_id = institution.id

    account_id = next_primary_key(session, Account.__table__)
    changes.append(
        Change(
            "account",
            str(account_id),
            None,
            {
                "id": account_id,
                "institution_id": institution_id,
                "name": name,
                "type": type_value,
                "last4": last4,
                "joint": joint,
                "closed": False,
            },
        )
    )
    changes.append(_alias_change(session, institution_id, account_id, statement_account_id))
    provider_id = _sole_provider_id(session, institution_id)
    if provider_id is not None:
        link_id = next_primary_key(session, ProviderAccount.__table__)
        changes.append(
            Change(
                "provider_account",
                str(link_id),
                None,
                {"id": link_id, "provider_id": provider_id, "account_id": account_id},
            )
        )
    preview = make_preview(
        session,
        "create_account",
        changes,
        summary={"institution": institution_key, "account_last4": last4},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def add_alias(
    session: Session,
    *,
    account_id: int,
    institution_key: str,
    statement_account_id: str,
    idempotency_key: str,
) -> Batch:
    """Map another statement account ID to an existing account, as a batch.

    Used when a card is reissued under a new number. Raises AccountError if the institution
    or account is unknown, the account belongs to another institution, or the ID is mapped.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    institution = session.scalars(
        select(Institution).where(Institution.key == institution_key)
    ).first()
    if institution is None:
        raise AccountError(f"there is no institution {institution_key!r}")
    account = session.get(Account, account_id)
    if account is None or account.institution_id != institution.id:
        raise AccountError(f"account {account_id} is not at {institution_key}")
    if find_account(session, institution_key, statement_account_id) is not None:
        raise AccountError(
            f"a statement account ending in {last4_of(statement_account_id)} "
            "is already mapped to an account"
        )

    change = _alias_change(session, institution.id, account_id, statement_account_id)
    preview = make_preview(
        session,
        "add_alias",
        [change],
        summary={"institution": institution_key, "account_id": account_id},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def _sole_provider_id(session: Session, institution_id: int) -> int | None:
    """The institution's only provider, or None when it has none or several."""
    ids = session.scalars(
        select(Provider.id).where(Provider.institution_id == institution_id).limit(2)
    ).all()
    return ids[0] if len(ids) == 1 else None


def _alias_change(
    session: Session, institution_id: int, account_id: int, statement_account_id: str
) -> Change:
    alias_id = next_primary_key(session, AccountAlias.__table__)
    return Change(
        "account_alias",
        str(alias_id),
        None,
        {
            "id": alias_id,
            "institution_id": institution_id,
            "account_id": account_id,
            "statement_account_id": statement_account_id,
        },
    )


@dataclass(frozen=True)
class NewAccount:
    """An account to create before any statement file names it. last4 is its four digits."""

    last4: str
    name: str
    type: AccountType | str
    joint: bool = False


def create_accounts_without_alias(
    session: Session,
    *,
    institution_key: str,
    institution_name: str | None,
    accounts: Sequence[NewAccount],
    last4_is_statement_id: bool,
    provider_id: int | None = None,
    idempotency_key: str,
) -> Batch:
    """Create accounts that have no statement account ID yet, as one batch.

    The institution is created in the same batch when it does not exist, and then institution_name
    is required. With last4_is_statement_id, each account also gets an alias equal to its last four
    digits, because the institution names its accounts that way (BoA's CSV folders). Otherwise the
    account stays without an alias until a file names it. No ID is invented. With provider_id, each
    new account also joins that provider, so the provider's next sync fetches it.

    Raises AccountError, and writes nothing, when an account's last four are not four digits, its
    name is blank or too long, two accounts share the same last four, an open account here already
    has them (a site would find two accounts), or the last four is already a statement account ID
    at this institution. Raises ValueError for an unknown type or an empty list.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if not accounts:
        raise ValueError("no accounts to create")

    cleaned = [_checked_new_account(new) for new in accounts]
    seen: set[str] = set()
    for new in cleaned:
        if new.last4 in seen:
            raise AccountError(f"two accounts end in ••{new.last4}; each needs its own")
        seen.add(new.last4)

    changes: list[Change] = []
    institution = session.scalars(
        select(Institution).where(Institution.key == institution_key)
    ).first()
    if institution is None:
        if institution_name is None:
            raise AccountError(f"there is no institution {institution_key!r} yet")
        institution_id = next_primary_key(session, Institution.__table__)
        changes.append(
            Change(
                "institution",
                str(institution_id),
                None,
                {"id": institution_id, "key": institution_key, "name": institution_name},
            )
        )
    else:
        institution_id = institution.id
        open_last4 = set(
            session.scalars(
                select(Account.last4).where(
                    Account.institution_id == institution_id,
                    Account.closed.is_(False),
                    Account.last4.in_(sorted(seen)),
                )
            ).all()
        )
        if open_last4:
            raise AccountError(
                f"an open account here already ends in ••{sorted(open_last4)[0]}; "
                "use it, or rename the one you have"
            )
    if last4_is_statement_id:
        for new in cleaned:
            if find_account(session, institution_key, new.last4) is not None:
                raise AccountError(f"a statement account ending in ••{new.last4} is already mapped")

    account_base = next_primary_key(session, Account.__table__)
    alias_base = next_primary_key(session, AccountAlias.__table__)
    alias_count = 0
    for offset, new in enumerate(cleaned):
        account_id = account_base + offset
        changes.append(
            Change(
                "account",
                str(account_id),
                None,
                {
                    "id": account_id,
                    "institution_id": institution_id,
                    "name": new.name,
                    "type": new.type,
                    "last4": new.last4,
                    "joint": new.joint,
                    "closed": False,
                },
            )
        )
        if last4_is_statement_id:
            alias_id = alias_base + alias_count
            alias_count += 1
            changes.append(
                Change(
                    "account_alias",
                    str(alias_id),
                    None,
                    {
                        "id": alias_id,
                        "institution_id": institution_id,
                        "account_id": account_id,
                        "statement_account_id": new.last4,
                    },
                )
            )
    if provider_id is not None:
        link_base = next_primary_key(session, ProviderAccount.__table__)
        for offset in range(len(cleaned)):
            link_id = link_base + offset
            changes.append(
                Change(
                    "provider_account",
                    str(link_id),
                    None,
                    {
                        "id": link_id,
                        "provider_id": provider_id,
                        "account_id": account_base + offset,
                    },
                )
            )
    preview = make_preview(
        session,
        "accounts_create",
        changes,
        summary={"institution": institution_key, "accounts": len(cleaned)},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def _checked_new_account(new: NewAccount) -> NewAccount:
    last4 = new.last4.strip()
    if not _LAST4.fullmatch(last4):
        raise AccountError("the last four digits must be exactly four digits")
    name = new.name.strip()
    if not name or len(name) > NAME_MAX:
        raise AccountError(f"an account name is 1 to {NAME_MAX} characters")
    account_type = AccountType(new.type).value
    return NewAccount(last4=last4, name=name, type=account_type, joint=bool(new.joint))
