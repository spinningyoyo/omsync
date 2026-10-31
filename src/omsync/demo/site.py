"""The demo's copy of the data: a pristine built tree that a reset copies back over the working one.

The server works in a temporary copy, so every change a visitor makes is thrown away when the
demo resets or the server stops. Resetting deletes only inside a directory that carries the demo
marker, so a configured data directory can never be the one emptied.
"""

import shutil
from pathlib import Path

from sqlalchemy.engine import Engine

from omsync.demo import is_demo_dir

# The made-up person the demo signs in as. It is shown in the user menu.
DEMO_EMAIL = "kunchorn@example.com"


class DemoSite:
    """Holds the pristine demo tree and puts it back on reset."""

    def __init__(self, pristine: Path) -> None:
        self.pristine = pristine

    def reset(self, engine: Engine, data_dir: Path) -> None:
        """Replace the working data with a fresh copy of the pristine tree.

        The engine is disposed first so no connection holds the database file. Raises ValueError
        when data_dir is not a demo directory, and writes nothing in that case.
        """
        if not is_demo_dir(data_dir):
            raise ValueError(f"{data_dir} is not a demo directory; refusing to reset it")
        engine.dispose()
        for child in data_dir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        shutil.copytree(self.pristine, data_dir, dirs_exist_ok=True)
