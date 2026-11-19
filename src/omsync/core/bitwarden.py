"""The Bitwarden vault, through the `bw` CLI: an idle-timed session and reads of bank logins.

The session key and the master password reach `bw` only through one child process's
environment.
"""

import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from omsync.core.vault import (
    Keychain,
    LoginCredentials,
    LoginSummary,
    Vault,
    VaultError,
    VaultLocked,
)

CLIENT_ID_ENTRY = "bitwarden-client-id"
CLIENT_SECRET_ENTRY = "bitwarden-client-secret"
MISSING_API_KEY = "Bitwarden API key missing from keychain; run omsync doctor"
MISSING_EMAIL = (
    "No Bitwarden email is set. Connect the vault on the Bank sync page, or run: "
    "omsync config set email <your-email>"
)
LOGIN_ITEM_TYPE = 1

_PASSWORD_ENV = "OMSYNC_BW_PASSWORD"
_INHERITED_SECRET_VARS = ("BW_SESSION", "BW_CLIENTID", "BW_CLIENTSECRET", "BW_PASSWORD")
_STDERR_LIMIT = 200


class BitwardenCLI:
    """Runs the `bw` executable. Each call gets its own environment copy."""

    def __init__(
        self,
        executable: str = "bw",
        *,
        appdata_dir: Path,
        timeout: float = 60,
    ) -> None:
        self._executable = executable
        self._appdata_dir = Path(appdata_dir)
        self._timeout = timeout

    def run(
        self,
        args: list[str],
        *,
        extra_env: Mapping[str, str] | None = None,
        input: str | None = None,
    ) -> str:
        secrets = dict(extra_env or {})
        env = {k: v for k, v in os.environ.items() if k not in _INHERITED_SECRET_VARS}
        env["BITWARDENCLI_APPDATA_DIR"] = str(self._appdata_dir)
        env["BW_NOINTERACTION"] = "true"
        env.update(secrets)
        try:
            # Windows starts only an .exe by bare name; npm installs bw as bw.cmd.
            executable = shutil.which(self._executable) or self._executable
            completed = subprocess.run(
                [executable, *args],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout,
                input=input,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise VaultError(f"Bitwarden CLI '{args[0]}' timed out") from None
        except OSError:
            raise VaultError(f"Bitwarden CLI '{self._executable}' could not be started") from None
        if completed.returncode != 0 or (not completed.stdout.strip() and completed.stderr.strip()):
            detail = _sanitize(completed.stderr, secrets)
            detail = detail or f"exit status {completed.returncode}"
            raise VaultError(f"Bitwarden CLI '{args[0]}' failed: {detail}")
        return completed.stdout

    def status(self) -> dict[str, Any]:
        data = _parse_json(self.run(["status"]), "status")
        if not isinstance(data, dict):
            raise VaultError("Unexpected Bitwarden status output")
        return data


class BitwardenVault(Vault):
    """A Bitwarden session for one account email. The clock is injectable for tests."""

    def __init__(
        self,
        cli: BitwardenCLI,
        keychain: Keychain,
        idle_minutes: int,
        email: str | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cli = cli
        self._keychain = keychain
        self._idle_minutes = idle_minutes
        self._email = (email or "").strip()
        self._clock = clock
        self._session: str | None = None
        self._last_used = 0.0

    def __repr__(self) -> str:
        return (
            f"BitwardenVault(idle_minutes={self._idle_minutes}, "
            f"unlocked={self._session is not None})"
        )

    @property
    def idle_minutes(self) -> int:
        return self._idle_minutes

    def unlock(self, password: str) -> None:
        if not self._email:
            raise VaultError(MISSING_EMAIL)
        self._ensure_login()
        try:
            self._session = self._cli_unlock(password)
        except VaultError as exc:
            if _is_bad_password(exc):
                raise VaultError("Incorrect master password") from None
            self._relogin()
            try:
                self._session = self._cli_unlock(password)
            except VaultError as retry_exc:
                if _is_bad_password(retry_exc):
                    raise VaultError("Incorrect master password") from None
                raise
        self._last_used = self._clock()

    def is_unlocked(self) -> bool:
        if self._session is None:
            return False
        if self._idle_minutes > 0 and self._clock() - self._last_used > self._idle_minutes * 60:
            self.lock()
            return False
        return True

    def lock(self) -> None:
        with contextlib.suppress(VaultError):
            self._cli.run(["lock"])
        self._session = None

    def disconnect(self) -> None:
        self._session = None
        with contextlib.suppress(VaultError):
            self._cli.run(["logout"])

    def find_logins(self, search: str) -> list[LoginSummary]:
        key = self._require_session()
        output = self._cli.run(["list", "items", "--search", search], extra_env={"BW_SESSION": key})
        items = _parse_json(output, "item list")
        if not isinstance(items, list):
            raise VaultError("Unexpected Bitwarden item list")
        self._last_used = self._clock()
        return [
            LoginSummary(
                id=str(item["id"]),
                name=str(item.get("name") or ""),
                username=(item.get("login") or {}).get("username") or None,
            )
            for item in items
            if isinstance(item, dict) and item.get("type") == LOGIN_ITEM_TYPE
        ]

    def get_login(self, item_id: str) -> LoginCredentials:
        key = self._require_session()
        output = self._cli.run(["get", "item", item_id], extra_env={"BW_SESSION": key})
        item = _parse_json(output, "item")
        if not isinstance(item, dict):
            raise VaultError("Unexpected Bitwarden item output")
        login = item.get("login") or {}
        password = login.get("password")
        if not password:
            raise VaultError("Bitwarden item has no password")
        self._last_used = self._clock()
        return LoginCredentials(
            id=str(item.get("id") or item_id),
            name=str(item.get("name") or ""),
            username=login.get("username") or None,
            password=str(password),
            totp=login.get("totp") or None,
            uris=_login_uris(login),
        )

    def _require_session(self) -> str:
        if not self.is_unlocked() or self._session is None:
            raise VaultLocked("Vault is locked; unlock it first")
        return self._session

    def _ensure_login(self) -> None:
        try:
            status = self._cli.status()
            if status.get("status") != "unauthenticated" and _same_email(
                status.get("userEmail"), self._email
            ):
                return
        except VaultError:
            pass
        self._relogin()

    def _relogin(self) -> None:
        client_id = self._keychain.get(CLIENT_ID_ENTRY)
        client_secret = self._keychain.get(CLIENT_SECRET_ENTRY)
        if not client_id or not client_secret:
            raise VaultError(MISSING_API_KEY)
        with contextlib.suppress(VaultError):
            self._cli.run(["logout"])
        self._cli.run(
            ["login", "--apikey"],
            extra_env={"BW_CLIENTID": client_id, "BW_CLIENTSECRET": client_secret},
        )
        if not _same_email(self._cli.status().get("userEmail"), self._email):
            raise VaultError("This email does not match the Bitwarden account of your API key")

    def _cli_unlock(self, password: str) -> str:
        output = self._cli.run(
            ["unlock", "--passwordenv", _PASSWORD_ENV, "--raw"],
            extra_env={_PASSWORD_ENV: password},
        )
        key = output.strip()
        if not key:
            raise VaultError("Bitwarden returned no session key")
        return key


def _is_bad_password(exc: VaultError) -> bool:
    return "invalid master password" in str(exc).lower()


def _login_uris(login: dict[str, Any]) -> tuple[str, ...]:
    """The login's URIs, in Bitwarden's order. Entries without a uri are skipped; none gives ()."""
    entries = login.get("uris") or []
    return tuple(
        str(entry["uri"]) for entry in entries if isinstance(entry, dict) and entry.get("uri")
    )


def _same_email(found: object, expected: str) -> bool:
    left = str(found or "").strip().lower()
    return bool(left) and left == expected.strip().lower()


def _sanitize(stderr: str | None, secrets: Mapping[str, str]) -> str:
    # Mask before truncating so a secret cut at the limit cannot leak a prefix.
    text = (stderr or "").strip()
    for value in secrets.values():
        if value:
            text = text.replace(value, "***")
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.startswith("at ") and line not in ("{", "}", "},"):
            return line[:_STDERR_LIMIT]
    return text[:_STDERR_LIMIT]


def _parse_json(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise VaultError(f"Could not read Bitwarden {what} output") from None
