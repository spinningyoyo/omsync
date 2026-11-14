"""Contacts: the people and businesses your transactions are with, and you.

A contact has a kind, a role, and a display name. A person's names are stored as given, middle, and
family names, and recognized in the forms banks print (see name_forms). Patterns recognize wordings
that no name covers, one list for payments you sent ("out") and one for payments you received
("in"); a pattern may be tied to one of the contact's accounts. A contact account is an account
outside the ledger, named in descriptions by its last four digits. You are the owner contact; a
joint account's other owner is the co-owner. Deleting a contact or an account clears the
counterparty that transactions and splits stored for it (see omsync.ledger.counterparty).

An account nickname is the name a payment app shows for a tracked account. It belongs to the
account, not to a contact, and is not an alias, which maps a statement's account ID at import.

Writes go through omsync.core.batches, so each change can be previewed and undone. The loaders
return frozen dataclasses, so reconciliation never holds a database row.
"""

from omsync.ledger.contacts.models import (
    MAX_ACCOUNTS_PER_CONTACT,
    MAX_NAME_LENGTH,
    MAX_NAMES_PER_CONTACT,
    MAX_NICKNAMES_PER_ACCOUNT,
    MAX_PATTERN_LENGTH,
    MAX_PATTERNS_PER_SIDE,
    SIDES,
    AccountNickname,
    Contact,
    ContactAccount,
    ContactKind,
    ContactName,
    ContactPattern,
    ContactRole,
    NothingChanged,
)
from omsync.ledger.contacts.reads import (
    ContactAccountView,
    ContactView,
    NameMatch,
    NicknameView,
    PatternView,
    PersonName,
    find_names,
    get_owner,
    list_nicknames,
    load_contacts,
    load_owner,
    name_forms,
    tokenize,
)
from omsync.ledger.contacts.validation import (
    check_pattern,
    clean_person_name,
    parse_person_name,
    starting_names,
)
from omsync.ledger.contacts.writes import (
    add_account_nickname,
    add_contact_account,
    add_name,
    add_owner_name,
    add_pattern,
    create_contact,
    delete_contact,
    merge_contacts,
    remove_account_nickname,
    remove_contact_account,
    remove_name,
    remove_pattern,
    set_contact_role,
    update_contact,
)

__all__ = [
    "MAX_ACCOUNTS_PER_CONTACT",
    "MAX_NAMES_PER_CONTACT",
    "MAX_NAME_LENGTH",
    "MAX_NICKNAMES_PER_ACCOUNT",
    "MAX_PATTERNS_PER_SIDE",
    "MAX_PATTERN_LENGTH",
    "SIDES",
    "AccountNickname",
    "Contact",
    "ContactAccount",
    "ContactAccountView",
    "ContactKind",
    "ContactName",
    "ContactPattern",
    "ContactRole",
    "ContactView",
    "NameMatch",
    "NicknameView",
    "NothingChanged",
    "PatternView",
    "PersonName",
    "add_account_nickname",
    "add_contact_account",
    "add_name",
    "add_owner_name",
    "add_pattern",
    "check_pattern",
    "clean_person_name",
    "create_contact",
    "delete_contact",
    "find_names",
    "get_owner",
    "list_nicknames",
    "load_contacts",
    "load_owner",
    "merge_contacts",
    "name_forms",
    "parse_person_name",
    "remove_account_nickname",
    "remove_contact_account",
    "remove_name",
    "remove_pattern",
    "set_contact_role",
    "starting_names",
    "tokenize",
    "update_contact",
]
