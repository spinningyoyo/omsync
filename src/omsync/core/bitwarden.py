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
    "No Bitwarden email is set. Connect the vault on the Data sync page, or run: "
    "omsync config set email <your-email>"
)
LOGIN_ITEM_TYPE = 1

_PASSWORD_ENV = "OMSYNC_BW_PASSWORD"
_INHERITED_SECRET_VARS = ("BW_SESSION", "BW_CLIENTID", "BW_CLIENTSECRET", "BW_PASSWORD")
_STDERR_LIMIT = 200
# Commands whose answer is on stdout. The others report only through their exit code.
_ANSWER_COMMANDS = frozenset({"unlock", "list", "get", "status"})
LOCKED_MESSAGE = "Vault is locked; unlock it first"
# The first release that flushes its output before it exits; older ones can lose the session key.
MIN_BW_VERSION = (2026, 10, 1)
UPDATE_BW = "update the Bitwarden CLI: brew upgrade bitwarden-cli"
EMAIL_MISMATCH = "This email does not match the Bitwarden account of your API key"


class CommandFailed(VaultError):
    """`bw` ran and reported failure: a non-zero exit, or no answer where one was due."""


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
        answer_missing = args[0] in _ANSWER_COMMANDS and not completed.stdout.strip()
        if completed.returncode != 0 or answer_missing:
            detail = _sanitize(completed.stderr, secrets)
            detail = detail or f"exit status {completed.returncode}"
            raise CommandFailed(f"Bitwarden CLI '{args[0]}' failed: {detail}")
        return completed.stdout

    def version(self) -> tuple[int, ...] | None:
        """The installed bw version, or None when it cannot be read."""
        try:
            return parse_version(self.run(["--version"]))
        except VaultError:
            return None

    def status(self, session: str | None = None) -> dict[str, Any]:
        extra_env = {"BW_SESSION": session} if session else None
        data = _parse_json(self.run(["status"], extra_env=extra_env), "status")
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
        status = self._cli.status()
        if status.get("status") == "unauthenticated":
            self._login()
        elif not _same_email(status.get("userEmail"), self._email):
            raise VaultError(EMAIL_MISMATCH)
        try:
            key = self._cli_unlock(password)
        except CommandFailed as failure:
            raise self._unlock_failure(failure) from None
        self._session = key
        self._last_used = self._clock()
        # Fresh logins come from the server. A failed refresh leaves the cached vault in use.
        with contextlib.suppress(VaultError):
            self._cli.run(["sync"], extra_env={"BW_SESSION": key})

    def is_unlocked(self) -> bool:
        """Memory only: this never runs `bw`. A session past its idle time counts as locked."""
        return self._session is not None and not self._idle_expired()

    def verify(self) -> None:
        key = self._require_session()
        try:
            status = self._cli.status(key)
        except CommandFailed:
            self._forget()
            raise VaultLocked(LOCKED_MESSAGE) from None
        if status.get("status") != "unlocked":
            self._forget()
            raise VaultLocked(LOCKED_MESSAGE)

    def lock(self) -> None:
        with contextlib.suppress(VaultError):
            self._cli.run(["lock"])
        self._session = None

    def expire(self) -> None:
        if self._session is not None and self._idle_expired():
            self.lock()

    def disconnect(self) -> None:
        self._session = None
        with contextlib.suppress(VaultError):
            self._cli.run(["logout"])

    def find_logins(self, search: str) -> list[LoginSummary]:
        output = self._read(["list", "items", "--search", search])
        items = _parse_json(output, "item list")
        if not isinstance(items, list):
            raise VaultError("Unexpected Bitwarden item list")
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
        output = self._read(["get", "item", item_id])
        item = _parse_json(output, "item")
        if not isinstance(item, dict):
            raise VaultError("Unexpected Bitwarden item output")
        login = item.get("login") or {}
        password = login.get("password")
        if not password:
            raise VaultError("Bitwarden item has no password")
        return LoginCredentials(
            id=str(item.get("id") or item_id),
            name=str(item.get("name") or ""),
            username=login.get("username") or None,
            password=str(password),
            totp=login.get("totp") or None,
            uris=_login_uris(login),
        )

    def _idle_expired(self) -> bool:
        return self._idle_minutes > 0 and self._clock() - self._last_used > self._idle_minutes * 60

    def _forget(self) -> None:
        self._session = None

    def _require_session(self) -> str:
        if self._session is None:
            raise VaultLocked(LOCKED_MESSAGE)
        if self._idle_expired():
            self.lock()
            raise VaultLocked(LOCKED_MESSAGE)
        return self._session

    def _read(self, args: list[str]) -> str:
        """Run a read with the session. A failure while `bw` itself says it is locked, or signed
        out, is VaultLocked, whatever the failure's wording."""
        key = self._require_session()
        try:
            output = self._cli.run(args, extra_env={"BW_SESSION": key})
        except CommandFailed:
            self._raise_if_locked(key)
            raise
        self._last_used = self._clock()
        return output

    def _raise_if_locked(self, key: str) -> None:
        try:
            status = self._cli.status(key)
        except VaultError:
            return
        if status.get("status") != "unlocked":
            self._forget()
            raise VaultLocked(LOCKED_MESSAGE) from None

    def _login(self) -> None:
        client_id = self._keychain.get(CLIENT_ID_ENTRY)
        client_secret = self._keychain.get(CLIENT_SECRET_ENTRY)
        if not client_id or not client_secret:
            raise VaultError(MISSING_API_KEY)
        self._cli.run(
            ["login", "--apikey"],
            extra_env={"BW_CLIENTID": client_id, "BW_CLIENTSECRET": client_secret},
        )
        if not _same_email(self._cli.status().get("userEmail"), self._email):
            raise VaultError(EMAIL_MISMATCH)
        # Unlock needs the account key that the first download brings; a login can end without it.
        try:
            self._cli.run(["sync"])
        except CommandFailed as failure:
            detail = str(failure).partition(": ")[2]
            raise VaultError(f"Bitwarden could not download the vault: {detail}") from None

    def _cli_unlock(self, password: str) -> str:
        output = self._cli.run(
            ["unlock", "--passwordenv", _PASSWORD_ENV, "--raw"],
            extra_env={_PASSWORD_ENV: password},
        )
        key = output.strip()
        if not key:
            raise VaultError("Bitwarden returned no session key")
        return key

    def _unlock_failure(self, failure: CommandFailed) -> VaultError:
        """Why `bw unlock` failed, in Bitwarden's own words when it is not a wrong password. The
        detail is masked stderr, so it never holds the password."""
        detail = str(failure).partition(": ")[2]
        if "invalid master password" in detail.lower():
            return VaultError("Incorrect master password")
        version = self._cli.version()
        if version is not None and version < MIN_BW_VERSION:
            detail = f"{detail}. If this keeps happening, {UPDATE_BW}"
        return VaultError(f"Bitwarden could not be unlocked: {detail}")


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


def parse_version(text: str) -> tuple[int, ...] | None:
    """`2026.11.0` as (2026, 11, 0). Anything else is None."""
    parts = text.strip().split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)
