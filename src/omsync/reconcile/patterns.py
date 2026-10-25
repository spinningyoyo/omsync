"""Transfer recognition patterns with literal alternatives for the user's own names."""

import re
from collections.abc import Iterable
from dataclasses import dataclass

_TRANSFER_BASE = (
    r"Online (?:scheduled |Banking )?[Tt]ransfer (?:to|from) (?:CHK )?\d{4}.*?Conf(?:irmation)?#",
    r"Online (?:Realtime )?Transfer (?:to|from|\d+).*transaction\s*#",
    r"Mobile transfer from (?:CHK )?\d{4}.*Conf(?:irmation)?#",
    r"\w[\w ]+\sDES:Extr?\w*",
    r"REAL TIME TRANSFER RECD FROM ABA/CONTR BNK-\d+.*REF:",
)
_CB_CREDIT_BASE = (r"DES:Credit\s+Bal",)
_GENERIC_BASE = (r"Credit Card Payment from \d{4}",)


@dataclass(frozen=True)
class Patterns:
    """The compiled patterns that depend on the user's own names."""

    transfer_with_conf: re.Pattern[str]
    cb_credit: re.Pattern[str]
    generic: re.Pattern[str]
    self_zelle: re.Pattern[str]
    self_inflow: re.Pattern[str]


_NEVER = re.compile(r"(?!)")


def build_patterns(own_names: Iterable[str]) -> Patterns:
    """Compile the name-dependent patterns, adding one alternative per name.

    Names are matched literally, not as regular expressions, and case-insensitively. A name's words
    may be separated by any spacing and an optional period, and a name never starts or ends in the
    middle of a word, so an initial does not match inside another name. Blank names are ignored.
    self_zelle matches a Zelle payment sent to one of your own names. self_inflow matches a Zelle
    payment received from one of your own names.
    """
    names = [name.strip() for name in own_names if name and name.strip()]
    if not names:
        return Patterns(
            transfer_with_conf=_compile(_TRANSFER_BASE),
            cb_credit=_compile(_CB_CREDIT_BASE),
            generic=_compile(_GENERIC_BASE),
            self_zelle=_NEVER,
            self_inflow=_NEVER,
        )
    alternation = _alternation(names)
    return Patterns(
        transfer_with_conf=_compile(
            (*_TRANSFER_BASE, rf"TRANSFER \w+ (?:\w+ )?(?:{alternation}):.*Confirmation#")
        ),
        cb_credit=_compile((*_CB_CREDIT_BASE, rf"DES:Payment.*INDN:.*(?:{alternation})")),
        generic=_compile((*_GENERIC_BASE, rf"^Transfer (?:{alternation})$")),
        self_zelle=_compile(
            (
                r"Zelle\s+(?:payment|Transfer|Recurring payment|Scheduled payment)\s+to\s+"
                rf"(?:{alternation})",
            )
        ),
        self_inflow=_compile(
            (
                r"Zelle\s+(?:payment|Transfer|Recurring payment|Scheduled payment)\s+from\s+"
                rf"(?:{alternation})",
            )
        ),
    )


def names_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """Compile one pattern that finds any of the names as build_patterns reads them, or None when
    there is no name."""
    cleaned = [name.strip() for name in names if name and name.strip()]
    return _compile((_alternation(cleaned),)) if cleaned else None


def _alternation(names: Iterable[str]) -> str:
    """One alternative per name, bounded so that it neither starts nor ends inside a word."""
    alternatives = (r"\.?\s+".join(re.escape(word) for word in name.split()) for name in names)
    return rf"(?<![A-Za-z])(?:{'|'.join(alternatives)})(?![A-Za-z])"


def _compile(alternatives: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(alternatives), re.IGNORECASE)


DEFAULT_PATTERNS = build_patterns(())

# Module-level names for code that does not run per user, such as tests of the base patterns.
TRANSFER_WITH_CONF_RE = DEFAULT_PATTERNS.transfer_with_conf
CB_CREDIT_RE = DEFAULT_PATTERNS.cb_credit
GENERIC_TRANSFER_RE = DEFAULT_PATTERNS.generic
