"""Amounts as HTML for the templates.

Privacy mode blurs every element with the `privacy-sensitive` class (styles/privacy-mode.css).
An amount that is shown as text goes through `sensitive` or `money_html`, so it carries the class.
Chart data and anything inside an attribute stay plain text, because the markup would break there.
"""

from markupsafe import Markup, escape

from omsync.core.money import format_cents

PRIVACY_CLASS = "privacy-sensitive"


def sensitive(text: str) -> Markup:
    """Text that shows an amount, wrapped so privacy mode can blur it."""
    return Markup(f'<span class="{PRIVACY_CLASS}">{escape(text)}</span>')


def money_html(cents: int) -> Markup:
    """An amount in cents, formatted for display and marked as sensitive."""
    return sensitive(format_cents(cents))
