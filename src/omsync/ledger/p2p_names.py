"""How a Zelle payment description names the other person."""

import re

ZELLE_PERSON_RES = {
    direction: re.compile(
        r"Zelle\s+(?:payment|Transfer|Recurring payment|Scheduled payment)\s+"
        rf"{direction}\s+(?P<rest>.*)",
        re.IGNORECASE,
    )
    for direction in ("to", "from")
}
NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'.-]*")
# Wordings that put the person after the confirmation: "Zelle Transfer Conf# ABC123; NAME" and
# Bank of America's "Online Banking Transfer Conf# ABC123; NAME" for a transfer from one of its
# other customers, which exists for money in only.
AFTER_CONFIRMATION_RE = re.compile(
    r"^\s*(?:Zelle\b[^;]*?|(?P<bank>Online Banking Transfer))\s*\bConf#\s*\w+\s*;\s*(?P<rest>.+)$",
    re.IGNORECASE,
)
NAME_END_RE = re.compile(r"\s+for\b|;", re.IGNORECASE)


def payment_name_text(description: str, amount_cents: int) -> str | None:
    """The part of a payment's description that names the other person, or None when it is not
    such a payment.

    A Zelle payment to someone has money out and one from someone has money in. The person follows
    "to" or "from", or in the older wordings follows the confirmation after a semicolon, in a
    Zelle Transfer or a Bank of America online transfer from another customer. The text stops
    before a memo ("for ...") or a semicolon, so a memo is never read as a name.
    """
    direction = "to" if amount_cents < 0 else "from"
    match = ZELLE_PERSON_RES[direction].search(description)
    rest = match.group("rest") if match is not None else None
    if rest is None:
        older = AFTER_CONFIRMATION_RE.match(description)
        if older is not None and (amount_cents > 0 or older.group("bank") is None):
            rest = older.group("rest")
    if rest is None:
        return None
    return NAME_END_RE.split(rest, maxsplit=1)[0].strip() or None


def written_name(name_text: str) -> str | None:
    """The person as the description writes them: the leading words of the name text, up to the
    first that is not a name word, or None when it has none."""
    words: list[str] = []
    for word in name_text.split():
        if not NAME_TOKEN_RE.fullmatch(word) or word.lower() == "conf":
            break
        words.append(word)
    return " ".join(words) or None
