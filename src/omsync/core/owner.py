"""The computer's owner check, which signs you in to omsync. omsync never sees a password.

macOS shows Touch ID, or the login password on a Mac without it. Windows shows Windows Hello,
or the login password on a computer without it.
"""

import asyncio
import getpass
import sys
import threading
from typing import Any, Protocol

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


def _setup_credui_signatures(
    credui: Any, advapi32: Any, kernel32: Any, ole32: Any, cred_ui_info_cls: type
) -> None:
    import ctypes
    from ctypes import wintypes

    credui.CredPackAuthenticationBufferW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
    ]
    credui.CredPackAuthenticationBufferW.restype = wintypes.BOOL

    credui.CredUIPromptForWindowsCredentialsW.argtypes = [
        ctypes.POINTER(cred_ui_info_cls),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.LPCVOID,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.ULONG),
        ctypes.POINTER(wintypes.BOOL),
        wintypes.DWORD,
    ]
    credui.CredUIPromptForWindowsCredentialsW.restype = wintypes.DWORD

    credui.CredUnPackAuthenticationBufferW.argtypes = [
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    credui.CredUnPackAuthenticationBufferW.restype = wintypes.BOOL

    advapi32.LogonUserW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.LogonUserW.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    ole32.CoTaskMemFree.argtypes = [wintypes.LPVOID]
    ole32.CoTaskMemFree.restype = None


def _pack_username(credui: Any, username: str) -> tuple[Any, int]:
    import ctypes
    from ctypes import wintypes

    if not username:
        return None, 0
    in_size = wintypes.DWORD(0)
    credui.CredPackAuthenticationBufferW(0, username, "", None, ctypes.byref(in_size))
    if in_size.value <= 0:
        return None, 0
    packed_buf = ctypes.create_string_buffer(in_size.value)
    if not credui.CredPackAuthenticationBufferW(0, username, "", packed_buf, ctypes.byref(in_size)):
        return None, 0
    return packed_buf, in_size.value


def _verify_logon(
    advapi32: Any, kernel32: Any, user: str, domain: str | None, password: str
) -> bool:
    import ctypes
    from ctypes import wintypes

    token = wintypes.HANDLE()
    # Try interactive logon first (for local accounts), then network logon
    logged_on = advapi32.LogonUserW(user, domain, password, 2, 0, ctypes.byref(token))
    if not logged_on:
        logged_on = advapi32.LogonUserW(user, domain, password, 3, 0, ctypes.byref(token))
    if logged_on:
        kernel32.CloseHandle(token)
        return True
    return False


def _confirm_windows_credentials(reason: str) -> bool:
    import ctypes
    from ctypes import wintypes

    class CredUiInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hwndParent", wintypes.HWND),
            ("pszMessageText", wintypes.LPCWSTR),
            ("pszCaptionText", wintypes.LPCWSTR),
            ("hbmBanner", wintypes.HBITMAP),
        ]

    try:
        credui = ctypes.WinDLL("credui.dll")
        advapi32 = ctypes.WinDLL("advapi32.dll")
        kernel32 = ctypes.WinDLL("kernel32.dll")
        ole32 = ctypes.WinDLL("ole32.dll")
    except OSError as exc:
        raise OwnerCheckUnavailable("Windows credential system is unavailable") from exc

    _setup_credui_signatures(credui, advapi32, kernel32, ole32, CredUiInfo)

    in_buf, in_size = _pack_username(credui, owner_name())

    ui_info = CredUiInfo()
    ui_info.cbSize = ctypes.sizeof(CredUiInfo)
    ui_info.hwndParent = None
    ui_info.pszCaptionText = "omsync"
    ui_info.pszMessageText = reason
    ui_info.hbmBanner = None

    auth_package = wintypes.ULONG(0)
    out_auth_buffer = wintypes.LPVOID()
    out_auth_size = wintypes.ULONG(0)
    save = wintypes.BOOL(False)
    creduiwin_generic = 0x0001

    res = credui.CredUIPromptForWindowsCredentialsW(
        ctypes.byref(ui_info),
        0,
        ctypes.byref(auth_package),
        in_buf,
        in_size,
        ctypes.byref(out_auth_buffer),
        ctypes.byref(out_auth_size),
        ctypes.byref(save),
        creduiwin_generic,
    )

    if res != 0 or not out_auth_buffer:
        return False

    max_len = 256
    user_buf = ctypes.create_unicode_buffer(max_len)
    domain_buf = ctypes.create_unicode_buffer(max_len)
    pass_buf = ctypes.create_unicode_buffer(max_len)
    u_len = wintypes.DWORD(max_len)
    d_len = wintypes.DWORD(max_len)
    p_len = wintypes.DWORD(max_len)

    try:
        unpacked = credui.CredUnPackAuthenticationBufferW(
            0,
            out_auth_buffer,
            out_auth_size.value,
            user_buf,
            ctypes.byref(u_len),
            domain_buf,
            ctypes.byref(d_len),
            pass_buf,
            ctypes.byref(p_len),
        )
        if not unpacked:
            return False

        domain_val = domain_buf.value or None
        return _verify_logon(advapi32, kernel32, user_buf.value, domain_val, pass_buf.value)
    finally:
        ctypes.memset(ctypes.byref(pass_buf), 0, ctypes.sizeof(pass_buf))
        ole32.CoTaskMemFree(out_auth_buffer)


class WindowsOwnerCheck:
    def confirm(self, reason: str) -> bool:
        return asyncio.run(self._confirm(reason))

    async def _confirm(self, reason: str) -> bool:
        try:
            from winrt.windows.security.credentials.ui import (
                UserConsentVerificationResult,
                UserConsentVerifier,
                UserConsentVerifierAvailability,
            )

            available = await UserConsentVerifier.check_availability_async()
            if available == UserConsentVerifierAvailability.AVAILABLE:
                verified = await UserConsentVerifier.request_verification_async(reason)
                if verified == UserConsentVerificationResult.VERIFIED:
                    return True
                if verified == UserConsentVerificationResult.CANCELED:
                    return False
        except (ImportError, OSError, RuntimeError):
            pass

        return _confirm_windows_credentials(reason)


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
