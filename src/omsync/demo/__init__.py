"""The demo: a made-up person's year of money, built in a disposable data directory.

Nothing here reads or writes the user's data directory. The builder writes synthetic statement
files and imports them through the same pipeline a real import uses, so the demo shows the app
working rather than a mock-up of it. A demo directory carries a marker file, and the server's demo
mode refuses any directory without one.
"""

from pathlib import Path

MARKER_NAME = "omsync-demo"


def is_demo_dir(path: Path) -> bool:
    """True when this directory was built by the demo builder."""
    return (path / MARKER_NAME).is_file()
