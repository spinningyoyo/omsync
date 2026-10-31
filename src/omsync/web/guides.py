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
PEACE = Group("Peace of mind", "amber")

GUIDES: tuple[Guide, ...] = (
    Guide(
        "tour",
        "A quick tour",
        "What omsync is, the screen you will use, and the made-up person the demo follows.",
        "compass",
        3,
        START,
    ),
    Guide(
        "statements",
        "Bring in your statements",
        "Where statement files go, how the Bank sync wizard maps them, and what a preview shows.",
        "file-up",
        4,
        START,
    ),
    Guide(
        "reading-amounts",
        "Reading amounts and balances",
        "Which way the signs run, why a card balance looks negative, and what counts in a report.",
        "scale",
        3,
        START,
    ),
    Guide(
        "categorize",
        "Categorize spending",
        "Category types, keywords, triage, and rules, and which choice wins when they disagree.",
        "wand-sparkles",
        5,
        EVERYDAY,
    ),
    Guide(
        "contacts",
        "People and the other side",
        "Who is on the other end of a payment, how omsync recognizes them, and what is owed.",
        "users-round",
        4,
        EVERYDAY,
    ),
    Guide(
        "reconcile",
        "Reconcile related money",
        "Transfers, card payments, and payments to people: what links itself, and what waits.",
        "handshake",
        3,
        EVERYDAY,
    ),
    Guide(
        "plan",
        "Budgets, goals, and recurring",
        "Flex and category budgets, goals linked to an account, and the bills that repeat.",
        "land-plot",
        5,
        PLANNING,
    ),
    Guide(
        "safety",
        "Undo, backups, and privacy",
        "How each change can be previewed and undone, how to back up, and what stays on this Mac.",
        "shield-check",
        3,
        PEACE,
    ),
    Guide(
        "own-ledger",
        "Start your own ledger",
        "Set up omsync with your own accounts, statements, and vault login.",
        "hard-drive",
        3,
        PEACE,
    ),
)
GROUPS: tuple[Group, ...] = (START, EVERYDAY, PLANNING, PEACE)


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
