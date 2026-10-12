"""Every test runs with a throwaway HOME, no OMSYNC_* variables, and no keychain, so nothing can
reach ~/omsync-data, ~/.config/omsync, or real credentials. A test that needs a secret
monkeypatches keyring itself, which overrides the guard."""

import os
from pathlib import Path

import keyring
import pytest

_REAL_HOME = Path.home()
_PLAYWRIGHT_CACHES = (
    _REAL_HOME / "Library" / "Caches" / "ms-playwright",
    _REAL_HOME / ".cache" / "ms-playwright",
)


def _no_keychain(*args: object, **kwargs: object) -> None:
    raise RuntimeError("tests must not use the real keychain; monkeypatch keyring instead")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    # Tool caches keep their real locations, so uv and Playwright do not download again.
    if not os.environ.get("UV_CACHE_DIR"):
        monkeypatch.setenv("UV_CACHE_DIR", str(_REAL_HOME / ".cache" / "uv"))
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        for cache in _PLAYWRIGHT_CACHES:
            if cache.is_dir():
                monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
                break
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    for name in [name for name in os.environ if name.startswith("OMSYNC_")]:
        monkeypatch.delenv(name)
    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, _no_keychain)
