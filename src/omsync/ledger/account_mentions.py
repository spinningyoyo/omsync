"""How a transaction description names an account by its last four digits."""

import re

# Ways a description names an account by its last four digits. A reference needs a marker, so a
# year or a store number is not one: a mask ("########4321", "CHK ...1111"), an account word
# ("ACCT# 2222", "CHK 3333", "account ending in 1234"), or "to"/"from" followed by up to three
# words ("to BOA EXAMPLE 4321"). Only the last form says which way the money went.
MASKED_REF = re.compile(r"(?:[#*]{3,}|X{3,}|\.{3})\s*(\d{4})(?!\d)", re.IGNORECASE)
LABELED_REF = re.compile(
    r"\b(?:ACCT|ACCOUNT|CHK|CHECKING|SAV|SAVINGS|CARD)\b\s*#?\s*(?:ENDING(?:\s+IN)?\s*)?(\d{4})(?!\d)",
    re.IGNORECASE,
)
DIRECTIONAL_REF = re.compile(
    r"\b(to|from)\s+(?:[A-Za-z][\w&'.-]*\s+){0,3}?(?:[#*X.]{3,}\s*)?(\d{4})(?!\d)",
    re.IGNORECASE,
)


def account_mentions(text: str) -> frozenset[str]:
    """Return the last-four digit groups a description uses to name an account."""
    return frozenset(
        match.group(match.lastindex)
        for pattern in (MASKED_REF, LABELED_REF, DIRECTIONAL_REF)
        for match in pattern.finditer(text or "")
        if match.lastindex
    )
