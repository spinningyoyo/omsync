"""Opaque row tokens for the add-institution wizard's mapping forms.

A mapping form names each discovered statement account by a random token, never by its
statement account ID, so the ID stays on the server. A token is kept in memory for a short time,
belongs to one institution, and is refused for any other. Tokens are not persisted, and a restart
ends them, as it ends the app session.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_TTL_SECONDS = 30 * 60
MAX_TOKENS = 500


@dataclass(frozen=True)
class _Entry:
    institution_key: str
    statement_account_id: str
    expires_at: float


class MappingTokens:
    """Issues tokens for discovered statement accounts and resolves them back, within the TTL."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    def issue(self, institution_key: str, statement_account_id: str) -> str:
        """A new token for one statement account at an institution."""
        self._prune()
        token = uuid.uuid4().hex
        self._entries[token] = _Entry(
            institution_key, statement_account_id, self._clock() + self._ttl
        )
        while len(self._entries) > MAX_TOKENS:
            self._entries.pop(next(iter(self._entries)))
        return token

    def resolve(self, token: str, institution_key: str) -> str | None:
        """The statement account ID a token names, or None if it is unknown, expired, or was
        issued for another institution."""
        self._prune()
        entry = self._entries.get(token)
        if entry is None or entry.institution_key != institution_key:
            return None
        return entry.statement_account_id

    def revoke_institution(self, institution_key: str) -> None:
        """Forget every token issued for an institution, when its rows are listed again."""
        for token in [t for t, e in self._entries.items() if e.institution_key == institution_key]:
            del self._entries[token]

    def _prune(self) -> None:
        now = self._clock()
        for token in [t for t, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[token]
