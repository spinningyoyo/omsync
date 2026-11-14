"""The Guides in Settings: the registry of walkthroughs, in reading order.

Each guide's text is a template in templates/guides/<slug>.html. This module holds what the index
and the guide pages need beyond that text: the title, summary, read time, icon, and group. It also
finds the screenshots that the guide figures show, so a figure with no image yet draws a
placeholder.
"""

from dataclasses import dataclass
from pathlib import Path

FIGURES_DIR = Path(__file__).resolve().parent / "static" / "img" / "guides"


@dataclass(frozen=True)
class Group:
    title: str
    tone: str  # a colour from the design tokens, used for the card icons of the group


@dataclass(frozen=True)
class Guide:
    slug: str
    title: str
    summary: str
    icon: str
    minutes: int
    group: Group


START = Group("Start here", "blue")
EVERYDAY = Group("Everyday", "green")
PLANNING = Group("Planning", "violet")

GUIDES: tuple[Guide, ...] = (
    Guide(
        "start",
        "Start here",
        "What omsync does, how amounts read, and the demo.",
        "compass",
        1,
        START,
    ),
    Guide(
        "statements",
        "Statements and bank sync",
        "The banks, where files go, and rows that need review.",
        "file-up",
        1,
        START,
    ),
    Guide(
        "categories",
        "Categories",
        "Category types, what sets a category, and rules.",
        "wand-sparkles",
        2,
        EVERYDAY,
    ),
    Guide(
        "links",
        "Links and contacts",
        "Transfers and payments that pair up, and what contacts owe.",
        "handshake",
        1,
        EVERYDAY,
    ),
    Guide(
        "plan",
        "Budgets, goals, and recurring",
        "Flex and category budgets, goals, and bills that repeat.",
        "land-plot",
        2,
        PLANNING,
    ),
)
GROUPS: tuple[Group, ...] = (START, EVERYDAY, PLANNING)


def guide_for(slug: str) -> Guide | None:
    return next((guide for guide in GUIDES if guide.slug == slug), None)


def neighbours(slug: str) -> tuple[Guide | None, Guide | None]:
    """The guides before and after this one in reading order, for the foot of a page."""
    index = next(position for position, guide in enumerate(GUIDES) if guide.slug == slug)
    before = GUIDES[index - 1] if index > 0 else None
    after = GUIDES[index + 1] if index + 1 < len(GUIDES) else None
    return before, after


def grouped_guides() -> list[tuple[Group, list[Guide]]]:
    return [(group, [guide for guide in GUIDES if guide.group == group]) for group in GROUPS]


def figure_files() -> dict[str, dict[str, tuple[int, int]]]:
    """For each figure name, the width and height of its light and dark screenshots, by theme. A
    figure with neither shows a placeholder until the screenshots are made."""
    if not FIGURES_DIR.is_dir():
        return {}
    found: dict[str, dict[str, tuple[int, int]]] = {}
    for path in FIGURES_DIR.glob("*.png"):
        name, _, theme = path.stem.rpartition("-")
        if theme in ("light", "dark"):
            found.setdefault(name, {})[theme] = _png_size(path)
    return found


def _png_size(path: Path) -> tuple[int, int]:
    """A PNG's width and height, read from its IHDR chunk, which always comes first."""
    with path.open("rb") as handle:
        header = handle.read(24)
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
