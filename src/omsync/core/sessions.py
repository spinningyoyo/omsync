"""In-memory app sessions and the sign-in rate limiter. Nothing here is persisted.

An app session is separate from the vault. Locking the vault never ends an app session, and
ending an app session never touches the vault. The clock is injectable for tests.
"""

import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

DEFAULT_IDLE_HOURS = 12.0
DEFAULT_MAX_FAILURES = 5
DEFAULT_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class AppSession:
    email: str
    last_seen: float


class SessionStore:
    """Opaque tokens for signed-in browsers. A session expires after `idle_hours` without use.

    Every successful `get` counts as use, so the expiry slides forward with activity.
    """

    def __init__(
        self,
        *,
        idle_hours: float = DEFAULT_IDLE_HOURS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_hours <= 0:
            raise ValueError("idle_hours must be positive")
        self._idle_seconds = idle_hours * 3600
        self._clock = clock
        self._sessions: dict[str, AppSession] = {}

    def create(self, email: str) -> str:
        """Start a session for `email` and return its token. The token is 43 URL-safe characters."""
        self._expire_idle()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = AppSession(email=email, last_seen=self._clock())
        return token

    def get(self, token: str) -> AppSession | None:
        """The live session for `token`, or None if it is unknown or has been idle too long."""
        session = self._sessions.get(token)
        if session is None:
            return None
        now = self._clock()
        if now - session.last_seen >= self._idle_seconds:
            del self._sessions[token]
            return None
        refreshed = replace(session, last_seen=now)
        self._sessions[token] = refreshed
        return refreshed

    def delete(self, token: str) -> None:
        self._sessions.pop(token, None)

    def clear(self) -> None:
        self._sessions.clear()

    def _expire_idle(self) -> None:
        now = self._clock()
        stale = [
            token
            for token, session in self._sessions.items()
            if now - session.last_seen >= self._idle_seconds
        ]
        for token in stale:
            del self._sessions[token]


class LoginRateLimiter:
    """Refuses sign-in attempts after `max_failures` failures within `window_seconds`.

    Only failures are recorded. A successful sign-in calls `reset`. The window is measured from
    each failure, so the limit lifts as the oldest counted failure ages out.
    """

    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be at least 1")
        self._max_failures = max_failures
        self._window = window_seconds
        self._clock = clock
        self._failures: deque[float] = deque()

    def is_blocked(self) -> bool:
        self._prune()
        return len(self._failures) >= self._max_failures

    def retry_after(self) -> float:
        """Seconds until another attempt is allowed. Zero when one is allowed now."""
        self._prune()
        if len(self._failures) < self._max_failures:
            return 0.0
        releases_at = self._failures[len(self._failures) - self._max_failures]
        return max(0.0, releases_at + self._window - self._clock())

    def record_failure(self) -> None:
        self._prune()
        self._failures.append(self._clock())

    def reset(self) -> None:
        self._failures.clear()

    def _prune(self) -> None:
        cutoff = self._clock() - self._window
        while self._failures and self._failures[0] <= cutoff:
            self._failures.popleft()
