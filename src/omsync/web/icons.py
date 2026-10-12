"""Inline Lucide icons for templates. Registered as the `icon_svg` Jinja global in app.py.

The files are vendored under static/icons (see ICONS.md). Each one is read once and cached.
Only names of the form [a-z0-9-]+ are accepted, so a name can never reach a path outside that
directory.
"""

import json
import re
from functools import cache
from pathlib import Path

from markupsafe import Markup, escape

from omsync.ledger.categories import DEFAULT_CATEGORY_ICON

ICONS_DIR = Path(__file__).resolve().parent / "static" / "icons"
ICON_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SVG = re.compile(r"<svg\b[^>]*>(?P<body>.*)</svg>", re.DOTALL)
_BETWEEN_TAGS = re.compile(r">\s+<")


@cache
def _icon_body(name: str) -> str:
    """The SVG children of a vendored icon. The file is read once, then served from the cache."""
    if not ICON_NAME.fullmatch(name):
        raise ValueError(f"Invalid icon name: {name!r}")
    text = _COMMENT.sub("", (ICONS_DIR / f"{name}.svg").read_text(encoding="utf-8"))
    match = _SVG.search(text)
    if match is None:
        raise ValueError(f"Icon {name!r} has no <svg> element")
    return _BETWEEN_TAGS.sub("><", match.group("body").strip())


def icon_svg(name: str, size: int = 20, class_: str = "") -> Markup:
    """An inline Lucide icon. It takes currentColor, so the surrounding text colour applies."""
    classes = " ".join(part for part in ("lucide", f"lucide-{name}", class_) if part)
    return Markup(
        f'<svg xmlns="http://www.w3.org/2000/svg" class="{escape(classes)}"'
        f' width="{int(size)}" height="{int(size)}" viewBox="0 0 24 24" fill="none"'
        ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
        f' stroke-linejoin="round" aria-hidden="true" focusable="false">{_icon_body(name)}</svg>'
    )


@cache
def _all_names() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in ICONS_DIR.glob("*.svg")))


@cache
def _tags() -> dict[str, tuple[str, ...]]:
    """Lucide's search tags for each icon, such as piggy-bank: savings, money."""
    raw = json.loads((ICONS_DIR / "tags.json").read_text(encoding="utf-8"))
    return {name: tuple(tags) for name, tags in raw.items()}


def icon_names() -> list[str]:
    """Every vendored icon name, sorted."""
    return list(_all_names())


def has_icon(name: str) -> bool:
    return bool(ICON_NAME.fullmatch(name)) and (ICONS_DIR / f"{name}.svg").is_file()


def category_icon_name(name: str | None) -> str:
    """The icon to draw for a category: its own, or the default if it has none or is missing."""
    return name if name and has_icon(name) else DEFAULT_CATEGORY_ICON


def search_icons(query: str, offset: int = 0, limit: int = 120) -> tuple[list[str], int]:
    """One page of the icons that match every word of the query, and how many match in all.

    A word matches a name, a part of a name, a tag, or a stretch inside either; see _rank for the
    order. An empty query matches everything, in name order.
    """
    words = query.lower().split()
    if not words:
        matches = list(_all_names())
    else:
        tags = _tags()
        ranked = []
        for name in _all_names():
            rank = _rank(name, tags.get(name, ()), words)
            if rank is not None:
                ranked.append((rank, name))
        matches = [name for _rank_, name in sorted(ranked)]
    return matches[offset : offset + limit], len(matches)


def _rank(name: str, tags: tuple[str, ...], words: list[str]) -> int | None:
    """Lower is better; None when a word matches nothing. A whole word beats a prefix, a prefix
    beats a tag, and a tag beats a stray run of letters inside a name."""
    parts = name.split("-")
    total = 0
    for word in words:
        if name == word:
            continue
        if word in parts:
            total += 1
        elif name.startswith(word) or any(part.startswith(word) for part in parts):
            total += 2
        elif word in tags:
            total += 3
        elif word in name:
            total += 4
        elif any(word in tag for tag in tags):
            total += 5
        else:
            return None
    return total
