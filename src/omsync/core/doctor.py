"""The checks `omsync doctor` runs, as data. The CLI prints them, and the Settings About page lists
them, so the two never disagree.

The keychain is checked by presence only: a value is read to test that it exists and is never put
in a message. Nothing here writes, and nothing here imports the CLI or the web layer.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from omsync.core.settings import Settings, resolve_config_path
from omsync.core.vault import CLIENT_ID_ENTRY, CLIENT_SECRET_ENTRY, KEYCHAIN_SERVICE

KEYCHAIN_ITEMS = (CLIENT_ID_ENTRY, CLIENT_SECRET_ENTRY)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    message: str

    def render(self) -> str:
        """The line `omsync doctor` prints for this check."""
        if self.ok:
            return f"OK   {self.name}: {self.message}"
        return f"MISSING {self.name}: {self.message}"


def run_checks(settings: Settings) -> list[Check]:
    """Every check, in the order `omsync doctor` prints them. Read-only."""
    checks = [_config_check(settings), _data_dir_check(settings), _bw_check()]
    checks += [_keychain_check(item) for item in KEYCHAIN_ITEMS]
    return checks


def _config_check(settings: Settings) -> Check:
    if settings.config_source is None:
        looked_at = resolve_config_path()
        return Check("config file", True, f"none at {looked_at}, using defaults")
    return Check("config file", True, str(settings.config_source))


def _data_dir_check(settings: Settings) -> Check:
    path: Path = settings.data_dir
    if not path.is_dir():
        return Check("data dir", False, f"{path} does not exist; run mkdir -p {path}")
    if not os.access(path, os.W_OK):
        return Check("data dir", False, f"{path} is not writable; check its permissions")
    return Check("data dir", True, f"{path} exists and is writable")


def _bw_check() -> Check:
    found = shutil.which("bw")
    if found is None:
        return Check("bw on PATH", False, "install the Bitwarden CLI and put bw on PATH")
    return Check("bw on PATH", True, found)


def _keychain_check(item: str) -> Check:
    name = f"keychain {item}"
    try:
        secret = keyring.get_password(KEYCHAIN_SERVICE, item)
    except KeyringError:
        secret = None
    if secret:  # presence only; the value is never printed
        return Check(name, True, "present")
    hint = f"not found; add it with: uv run keyring set {KEYCHAIN_SERVICE} {item}"
    return Check(name, False, hint)
