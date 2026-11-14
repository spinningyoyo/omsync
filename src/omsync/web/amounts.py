"""Amounts and dates as the web forms and templates handle them.

Output: privacy mode blurs every element with the `privacy-sensitive` class
(styles/privacy-mode.css). An amount that is shown as text goes through `sensitive` or
`money_html`, so it carries the class. Chart data and anything inside an attribute stay plain
text, because the markup would break there.

Input: the parse_* functions turn text a form typed into integer cents or a date. A ValueError
carries a sentence the form can show.
"""

from datetime import date

from markupsafe import Markup, escape

from omsync.core.money import format_cents, to_cents
from omsync.core.months import parse_day

PRIVACY_CLASS = "privacy-sensitive"


def sensitive(text: str) -> Markup:
    """Text that shows an amount, wrapped so privacy mode can blur it."""
    return Markup(f'<span class="{PRIVACY_CLASS}">{escape(text)}</span>')


def money_html(cents: int) -> Markup:
    """An amount in cents, formatted for display and marked as sensitive."""
    return sensitive(format_cents(cents))


def parse_cents(text: str, prompt: str) -> int:
    """An amount a form typed, in cents. A ValueError carries `prompt` when the text is not one."""
    try:
        return to_cents(text.strip())
    except (TypeError, ValueError):
        raise ValueError(prompt) from None


def parse_positive_cents(text: str) -> int:
    cents = parse_cents(text, "Enter the amount in dollars, such as 45.99.")
    if cents <= 0:
        raise ValueError("The amount must be more than zero.")
    return cents


def parse_tolerance_cents(text: str) -> int:
    cents = parse_cents(text, "Enter the exact tolerance in dollars, such as 1.00.")
    if cents < 0:
        raise ValueError("The tolerance cannot be negative.")
    return cents


def parse_date(text: str, prompt: str) -> date:
    """A date a form typed, as YYYY-MM-DD. A ValueError carries `prompt` when it is not one."""
    try:
        return parse_day(text)
    except ValueError:
        raise ValueError(prompt) from None


def parse_optional_date(text: str | None) -> date | None:
    """A date a form typed, or None when the field is blank or holds no date."""
    try:
        return parse_day(text) if text else None
    except ValueError:
        return None
