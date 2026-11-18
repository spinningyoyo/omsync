"""Machine settings: defaults, then omsync.toml, then OMSYNC_* environment variables."""

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("~/.config/omsync/omsync.toml")

_ENV_NAMES: dict[str, str] = {
    "OMSYNC_DATA_DIR": "data_dir",
    "OMSYNC_HOST": "host",
    "OMSYNC_PORT": "port",
    "OMSYNC_VAULT_IDLE_MINUTES": "vault_idle_minutes",
    "OMSYNC_EMAIL": "email",
    "OMSYNC_VAULT": "vault",
}
_TOML_KEYS = ("data_dir", "host", "port", "vault_idle_minutes", "email", "vault")
_WRITABLE_KEYS = ("data_dir", "port", "vault_idle_minutes", "email", "vault")

# The vaults omsync can read bank logins from. "none" means you sign in to each bank yourself.
VAULT_CHOICES = ("none", "bitwarden")

# omsync listens on this machine only. Remote access needs a design and security review first,
# so no setting may widen the bind address.
LOCAL_HOST = "127.0.0.1"


@dataclass(frozen=True)
class Settings:
    """Machine settings. Everything derived lives under data_dir.

    config_source is the TOML file that was actually read, or None when defaults were used.
    """

    data_dir: Path = field(default_factory=lambda: Path("~/omsync-data").expanduser())
    host: str = LOCAL_HOST
    port: int = 8765
    vault_idle_minutes: int = 15
    email: str | None = None  # the vault account's email, which unlocks the vault
    vault: str = "none"
    config_source: Path | None = field(default=None, compare=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "omsync.db"

    @property
    def statements_dir(self) -> Path:
        return self.data_dir / "statements"

    @property
    def receipts_dir(self) -> Path:
        return self.data_dir / "receipts"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def resolve_config_path(
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> Path:
    """Return the TOML path: the argument, then OMSYNC_CONFIG, then the default location."""
    if config_path is not None:
        return Path(config_path).expanduser()
    environ = os.environ if env is None else env
    if environ.get("OMSYNC_CONFIG"):
        return Path(environ["OMSYNC_CONFIG"]).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


def load_settings(
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> Settings:
    """Build settings with precedence defaults < TOML file < environment.

    `env` defaults to os.environ. A missing TOML file is not an error.
    Raises ValueError for an unreadable TOML file or an invalid value.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    values, source = _read_toml(resolve_config_path(environ, config_path))
    overrides: dict[str, Any] = {k: v for k, v in values.items() if k in _TOML_KEYS}
    for env_name, key in _ENV_NAMES.items():
        if environ.get(env_name):
            overrides[key] = environ[env_name]

    settings = Settings(
        **{key: _coerce(key, raw) for key, raw in overrides.items()},
        config_source=source,
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    if settings.host != LOCAL_HOST:
        raise ValueError(
            f"host must be {LOCAL_HOST}, got {settings.host!r}: omsync serves this Mac only"
        )
    if not 1 <= settings.port <= 65535:
        raise ValueError(f"port must be between 1 and 65535, got {settings.port}")
    if settings.vault not in VAULT_CHOICES:
        raise ValueError(f"vault must be one of {', '.join(VAULT_CHOICES)}, got {settings.vault!r}")
    if settings.vault_idle_minutes < 0:
        raise ValueError("vault_idle_minutes must not be negative")


def _read_toml(path: Path) -> tuple[dict[str, Any], Path | None]:
    """Return the parsed file and its path, or ({}, None) when the file is absent."""
    if not path.is_file():
        return {}, None
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle), path
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from exc


def _coerce(key: str, value: object) -> object:
    """Convert a raw TOML or environment value to the field's type."""
    if key == "data_dir":
        return Path(str(value)).expanduser()
    if key in ("host", "email"):
        return str(value)
    if key == "vault":
        return str(value).strip().lower()
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"{key} must be an integer, got {value!r}")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {value!r}") from exc


def setting_sources(
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
) -> list[tuple[str, str, str]]:
    """Each setting as (name, value, source), in the file's key order. The source is "default",
    "omsync.toml" or "environment"."""
    environ: Mapping[str, str] = os.environ if env is None else env
    settings = load_settings(environ, config_path)
    values, _ = _read_toml(resolve_config_path(environ, config_path))
    env_names = {key: name for name, key in _ENV_NAMES.items()}
    rows = []
    for key in _TOML_KEYS:
        if environ.get(env_names[key]):
            source = "environment"
        elif key in values:
            source = "omsync.toml"
        else:
            source = "default"
        value = getattr(settings, key)
        rows.append((key, "" if value is None else str(value), source))
    return rows


def update_settings(updates: Mapping[str, str], config_path: Path | None = None) -> Path:
    """Write settings to omsync.toml, creating the file and its folder if needed, and return its
    path. Raises ValueError for a key that cannot be written, an invalid value, or a file with keys
    omsync does not know, because it could not write them back unchanged."""
    refused = [key for key in updates if key not in _WRITABLE_KEYS]
    if refused:
        allowed = ", ".join(_WRITABLE_KEYS)
        raise ValueError(f"{refused[0]!r} is not a writable setting (choose from {allowed})")
    path = resolve_config_path(config_path=config_path)
    values, _ = _read_toml(path)
    unknown = [key for key in values if key not in _TOML_KEYS]
    if unknown:
        raise ValueError(f"{path} has keys omsync does not know: {', '.join(unknown)}")
    values.update(updates)
    _validate(Settings(**{key: _coerce(key, raw) for key, raw in values.items()}))
    lines = [f"{key} = {_toml_value(key, values[key])}" for key in _TOML_KEYS if key in values]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _toml_value(key: str, value: object) -> str:
    if key in ("port", "vault_idle_minutes"):
        return str(_coerce(key, value))
    # A JSON string is a valid TOML basic string when non-ASCII characters stay unescaped.
    return json.dumps(str(value), ensure_ascii=False)
