"""Where the Alembic migrations live, and the revisions they define.

The migrations ship inside the package, so an installed wheel can migrate its database as a
source checkout can. Every caller resolves the directory through MIGRATIONS_DIR.
"""

from argparse import Namespace
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

# core/schema.py -> core -> omsync: the migrations are a sibling of core/ in the package.
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def alembic_config(url: str) -> Config:
    """An Alembic config with no ini file. The `-x url=` override names the database to migrate."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.cmd_opts = Namespace(x=[f"url={url}"])
    return config


def head_revision() -> str:
    """The newest revision this omsync knows, or "none" when there are no migrations."""
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head() or "none"


def known_revisions() -> set[str]:
    """Every revision this omsync has a migration for. A database at any other revision is newer."""
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    return {revision.revision for revision in script.walk_revisions()}
