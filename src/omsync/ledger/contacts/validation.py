"""Checks on names, patterns, and labels before they are stored."""

import re

from omsync.ledger.contacts.models import MAX_NAME_LENGTH, MAX_PATTERN_LENGTH, ContactKind
from omsync.ledger.contacts.reads import PersonName, _plain


def clean_person_name(
    given: str, middle: str | None = None, family: str | None = None
) -> PersonName:
    """Check and clean a name's parts. A middle name needs a family name, and every part is at most
    80 characters. Raises ValueError otherwise."""
    parts = [" ".join(str(part or "").split()) for part in (given, middle, family)]
    clean_given, clean_middle, clean_family = parts
    if not _plain(clean_given):
        raise ValueError("Enter a name.")
    if clean_middle and not clean_family:
        raise ValueError("A middle name needs a family name too.")
    for part in parts:
        if len(part) > MAX_NAME_LENGTH:
            raise ValueError(f"A name can be at most {MAX_NAME_LENGTH} characters.")
    return PersonName(clean_given, clean_middle or None, clean_family or None)


def parse_person_name(text: str) -> PersonName:
    """Read a display name as a person's: the first word is the given name, the last the family
    name, and any words between them the middle name. One word is a given name only. Raises
    ValueError for an empty name or a part over 80 characters."""
    words = str(text).split()
    if len(words) < 2:
        return clean_person_name(" ".join(words))
    return clean_person_name(words[0], " ".join(words[1:-1]), words[-1])


def starting_names(
    name: str,
    kind: ContactKind | str,
    *,
    given: str = "",
    middle: str = "",
    family: str = "",
) -> list[PersonName]:
    """The names a new contact starts with: the parts typed, or with none the display name, read
    as a person's name for a person and whole for a business."""
    if given.strip() or middle.strip() or family.strip():
        return [clean_person_name(given, middle, family)]
    if ContactKind(kind) is ContactKind.business:
        return [clean_person_name(name)]
    return [parse_person_name(name)]


def check_pattern(pattern: str) -> None:
    """Raise ValueError unless the pattern is a valid regular expression of at most 200
    characters that cannot match the empty string, which would match every payee."""
    if not pattern:
        raise ValueError("Enter a pattern.")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"A pattern can be at most {MAX_PATTERN_LENGTH} characters.")
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{pattern!r} is not a valid pattern: {exc}") from exc
    if compiled.search(""):
        raise ValueError(f"{pattern!r} would match every payee. Make it more specific.")


def _clean_label(value: str, what: str) -> str:
    clean = " ".join(str(value).split())
    if not clean:
        raise ValueError(f"Enter {what}.")
    if len(clean) > MAX_NAME_LENGTH:
        raise ValueError(
            f"{what[0].upper()}{what[1:]} can be at most {MAX_NAME_LENGTH} characters."
        )
    return clean
