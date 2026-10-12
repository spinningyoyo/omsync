"""Institution logos for templates. Registered as the `institution_logo_url` Jinja global in app.py.

The logos are static files named for the institution keys they belong to. An institution key is
chosen by the person, so only keys with a file get a logo and every other institution falls back
to a monogram in the template. A business variant of an institution shares its logo.
"""

from pathlib import Path

LOGO_DIR = Path(__file__).resolve().parent / "static" / "img" / "institutions"
ALIASES = {"chase_business": "chase"}


def institution_logo_url(key: str) -> str | None:
    """The static URL of the institution's logo, or None when there is none."""
    name = ALIASES.get(key, key)
    if (LOGO_DIR / f"{name}.png").is_file():
        return f"/static/img/institutions/{name}.png"
    return None
