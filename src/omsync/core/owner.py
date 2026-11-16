"""The computer's owner check, which signs you in to omsync. omsync never sees a password.

macOS shows Touch ID, or the login password on a Mac without it. Windows shows Windows Hello.
"""

import asyncio
import getpass
import sys
import threading
from typing import Protocol

OWNER_CHECK_TIMEOUT = 120.0


class OwnerCheckUnavailable(Exception):
    """This computer cannot run the owner check. The message is safe to show."""


class OwnerCheck(Protocol):
    def confirm(self, reason: str) -> bool:
        """Show the system's prompt. True when the owner passed it, False when refused or
        cancelled."""
        ...


class MacOwnerCheck:
    def confirm(self, reason: str) -> bool:
        import LocalAuthentication as LA

        policy = LA.LAPolicyDeviceOwnerAuthentication
        context = LA.LAContext.new()
        available, _error = context.canEvaluatePolicy_error_(policy, None)
        if not available:
            raise OwnerCheckUnavailable("Set a login password for this Mac, then sign in again")
        done = threading.Event()
        result = {"passed": False}

        def reply(success: bool, _err: object) -> None:
            result["passed"] = bool(success)
            done.set()

        context.evaluatePolicy_localizedReason_reply_(policy, reason, reply)
        if not done.wait(OWNER_CHECK_TIMEOUT):
            context.invalidate()
            return False
        return result["passed"]


class WindowsOwnerCheck:
    def confirm(self, reason: str) -> bool:
        return asyncio.run(self._confirm(reason))

    async def _confirm(self, reason: str) -> bool:
        from winrt.windows.security.credentials.ui import (
            UserConsentVerificationResult,
            UserConsentVerifier,
            UserConsentVerifierAvailability,
        )

        available = await UserConsentVerifier.check_availability_async()
        if available != UserConsentVerifierAvailability.AVAILABLE:
            raise OwnerCheckUnavailable("Set up Windows Hello, then sign in again")
        verified = await UserConsentVerifier.request_verification_async(reason)
        return verified == UserConsentVerificationResult.VERIFIED


class NoOwnerCheck:
    def confirm(self, reason: str) -> bool:
        raise OwnerCheckUnavailable("Sign-in needs macOS or Windows")


def system_owner_check() -> OwnerCheck:
    if sys.platform == "darwin":
        return MacOwnerCheck()
    if sys.platform == "win32":
        return WindowsOwnerCheck()
    return NoOwnerCheck()


def owner_name() -> str:
    """The computer account's name, shown as who is signed in."""
    return getpass.getuser()
