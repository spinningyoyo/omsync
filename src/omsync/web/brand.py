"""The pixel mascot for templates. Registered as the `mascot_svg` Jinja global in app.py.

Inline the SVG shapes so the outline can take currentColor and the body can take the theme's
variable. The file comes from the omsync-nursery package. It is read once and then served from
the cache.
"""

import re
from functools import cache

from markupsafe import Markup, escape
from nursery.assets import MASCOT_SVG

MASCOT_PATH = MASCOT_SVG
_SVG = re.compile(r"<svg\b[^>]*>(?P<body>.*)</svg>", re.DOTALL)
_BETWEEN_TAGS = re.compile(r">\s+<")
_VIEWBOX = re.compile(r'viewBox="0 0 (\d+) (\d+)"')


@cache
def _source() -> tuple[str, int, int]:
    """The inner markup of mascot.svg, and its grid width and height from the viewBox."""
    text = MASCOT_PATH.read_text(encoding="utf-8")
    match = _SVG.search(text)
    view = _VIEWBOX.search(text)
    if match is None or view is None:
        raise ValueError(f"{MASCOT_PATH} has no <svg> element with a viewBox")
    inner = _BETWEEN_TAGS.sub("><", match.group("body").strip())
    return inner, int(view.group(1)), int(view.group(2))


def mascot_svg(width: int = 96, class_: str = "") -> Markup:
    """The mascot at a width in pixels. It is decorative, so it is hidden from screen readers."""
    inner, cols, rows = _source()
    classes = " ".join(part for part in ("brand-mascot", class_) if part)
    height = round(width * rows / cols)
    return Markup(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {cols} {rows}"'
        f' width="{int(width)}" height="{height}" class="{escape(classes)}"'
        ' shape-rendering="crispEdges" aria-hidden="true" focusable="false">'
        f"{inner}</svg>"
    )
