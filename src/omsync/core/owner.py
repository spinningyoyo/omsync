"""The computer's owner check, which signs you in to omsync. omsync never sees a password.

macOS shows Touch ID, or the login password on a Mac without it. Windows shows Windows Hello,
or its login password prompt where Hello cannot run. In both cases the system checks the answer.
omsync gets only yes or no: on Windows it passes the password prompt's sealed result straight to
the system logon call and never unpacks it.
"""

import asyncio
import functools
import getpass
import sys
import threading
from types import SimpleNamespace
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


_STATUS_LOGON_TYPE_NOT_GRANTED = 0xC000015B
_LOGON_INTERACTIVE = 2
_LOGON_NETWORK = 3
_CREDUIWIN_AUTHPACKAGE_ONLY = 0x10
_CREDUIWIN_ENUMERATE_CURRENT_USER = 0x200
_TOKEN_QUERY = 0x8
_TOKEN_USER_CLASS = 1


@functools.cache
def _win_types() -> SimpleNamespace:
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

    class LsaString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.c_char_p),
        ]

    class Luid(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class TokenSource(ctypes.Structure):
        _fields_ = [("SourceName", ctypes.c_char * 8), ("SourceIdentifier", Luid)]

    class QuotaLimits(ctypes.Structure):
        _fields_ = [
            ("PagedPoolLimit", ctypes.c_size_t),
            ("NonPagedPoolLimit", ctypes.c_size_t),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("PagefileLimit", ctypes.c_size_t),
            ("TimeLimit", ctypes.c_longlong),
        ]

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("User", SidAndAttributes)]

    return SimpleNamespace(
        CredUiInfo=CredUiInfo,
        LsaString=LsaString,
        Luid=Luid,
        TokenSource=TokenSource,
        QuotaLimits=QuotaLimits,
        TokenUser=TokenUser,
    )


def _declare(func: Any, argtypes: list[Any], restype: Any) -> None:
    func.argtypes = argtypes
    func.restype = restype


def _declare_lsa(secur32: Any, advapi32: Any, t: SimpleNamespace) -> None:
    import ctypes
    from ctypes import wintypes

    handle_p = ctypes.POINTER(wintypes.HANDLE)
    string_p = ctypes.POINTER(t.LsaString)
    ulong_p = ctypes.POINTER(wintypes.ULONG)
    _declare(secur32.LsaConnectUntrusted, [handle_p], wintypes.LONG)
    _declare(
        secur32.LsaLookupAuthenticationPackage,
        [wintypes.HANDLE, string_p, ulong_p],
        wintypes.LONG,
    )
    _declare(
        secur32.LsaLogonUser,
        [
            wintypes.HANDLE,
            string_p,
            wintypes.DWORD,
            wintypes.ULONG,
            wintypes.LPVOID,
            wintypes.ULONG,
            wintypes.LPVOID,
            ctypes.POINTER(t.TokenSource),
            ctypes.POINTER(wintypes.LPVOID),
            ulong_p,
            ctypes.POINTER(t.Luid),
            handle_p,
            ctypes.POINTER(t.QuotaLimits),
            ctypes.POINTER(wintypes.LONG),
        ],
        wintypes.LONG,
    )
    _declare(secur32.LsaFreeReturnBuffer, [wintypes.LPVOID], wintypes.LONG)
    _declare(secur32.LsaDeregisterLogonProcess, [wintypes.HANDLE], wintypes.LONG)
    _declare(advapi32.AllocateLocallyUniqueId, [ctypes.POINTER(t.Luid)], wintypes.BOOL)


def _declare_token(advapi32: Any, kernel32: Any) -> None:
    import ctypes
    from ctypes import wintypes

    _declare(kernel32.GetCurrentProcess, [], wintypes.HANDLE)
    _declare(kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
    _declare(
        advapi32.OpenProcessToken,
        [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
        wintypes.BOOL,
    )
    _declare(
        advapi32.GetTokenInformation,
        [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ],
        wintypes.BOOL,
    )
    _declare(advapi32.EqualSid, [ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL)


def _load_apis(t: SimpleNamespace) -> SimpleNamespace:
    import ctypes
    from ctypes import wintypes

    try:
        api = SimpleNamespace(
            credui=ctypes.WinDLL("credui.dll"),
            secur32=ctypes.WinDLL("secur32.dll"),
            advapi32=ctypes.WinDLL("advapi32.dll"),
            kernel32=ctypes.WinDLL("kernel32.dll"),
            ole32=ctypes.WinDLL("ole32.dll"),
        )
    except OSError as exc:
        raise OwnerCheckUnavailable("Windows credential system is unavailable") from exc

    _declare(
        api.credui.CredUIPromptForWindowsCredentialsW,
        [
            ctypes.POINTER(t.CredUiInfo),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.ULONG),
            wintypes.LPCVOID,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.LPVOID),
            ctypes.POINTER(wintypes.ULONG),
            ctypes.POINTER(wintypes.BOOL),
            wintypes.DWORD,
        ],
        wintypes.DWORD,
    )
    _declare(api.ole32.CoTaskMemFree, [wintypes.LPVOID], None)
    _declare_lsa(api.secur32, api.advapi32, t)
    _declare_token(api.advapi32, api.kernel32)
    return api


def _lsa_string(t: SimpleNamespace, text: bytes) -> Any:
    return t.LsaString(len(text), len(text) + 1, text)


def _connect_lsa(api: SimpleNamespace) -> Any:
    import ctypes
    from ctypes import wintypes

    lsa = wintypes.HANDLE()
    if api.secur32.LsaConnectUntrusted(ctypes.byref(lsa)) != 0:
        raise OwnerCheckUnavailable("Windows credential system is unavailable")
    return lsa


def _negotiate_package(api: SimpleNamespace, t: SimpleNamespace, lsa: Any) -> int:
    import ctypes
    from ctypes import wintypes

    package = wintypes.ULONG(0)
    name = _lsa_string(t, b"Negotiate")
    status = api.secur32.LsaLookupAuthenticationPackage(
        lsa, ctypes.byref(name), ctypes.byref(package)
    )
    if status != 0:
        raise OwnerCheckUnavailable("Windows credential system is unavailable")
    return package.value


def _prompt_credentials(
    api: SimpleNamespace, t: SimpleNamespace, reason: str, package_id: int
) -> tuple[int, int] | None:
    """Show the Windows password prompt for the current user. Returns the opaque credential
    buffer that Windows made and its size, or None when the owner cancelled."""
    import ctypes
    from ctypes import wintypes

    ui_info = t.CredUiInfo()
    ui_info.cbSize = ctypes.sizeof(t.CredUiInfo)
    ui_info.hwndParent = None
    ui_info.pszCaptionText = "omsync"
    ui_info.pszMessageText = reason
    ui_info.hbmBanner = None

    package = wintypes.ULONG(package_id)
    out_buffer = wintypes.LPVOID()
    out_size = wintypes.ULONG(0)
    save = wintypes.BOOL(False)
    flags = _CREDUIWIN_AUTHPACKAGE_ONLY | _CREDUIWIN_ENUMERATE_CURRENT_USER

    result = api.credui.CredUIPromptForWindowsCredentialsW(
        ctypes.byref(ui_info),
        0,
        ctypes.byref(package),
        None,
        0,
        ctypes.byref(out_buffer),
        ctypes.byref(out_size),
        ctypes.byref(save),
        flags,
    )
    if out_buffer.value is None:
        return None
    if result != 0:
        _release_credentials(api, out_buffer.value, out_size.value)
        return None
    return out_buffer.value, out_size.value


def _release_credentials(api: SimpleNamespace, buffer: int, size: int) -> None:
    import ctypes

    ctypes.memset(buffer, 0, size)
    api.ole32.CoTaskMemFree(buffer)


def _lsa_logon(
    api: SimpleNamespace,
    t: SimpleNamespace,
    lsa: Any,
    package_id: int,
    credentials: tuple[int, int],
    logon_type: int,
) -> tuple[int, Any]:
    """One LsaLogonUser call. Returns the unsigned NTSTATUS and the token handle value."""
    import ctypes
    from ctypes import wintypes

    origin = _lsa_string(t, b"omsync")
    source = t.TokenSource()
    source.SourceName = b"omsync\0\0"
    api.advapi32.AllocateLocallyUniqueId(ctypes.byref(source.SourceIdentifier))

    profile = wintypes.LPVOID()
    profile_size = wintypes.ULONG(0)
    logon_id = t.Luid()
    token = wintypes.HANDLE()
    quotas = t.QuotaLimits()
    sub_status = wintypes.LONG(0)
    status = api.secur32.LsaLogonUser(
        lsa,
        ctypes.byref(origin),
        logon_type,
        package_id,
        credentials[0],
        credentials[1],
        None,
        ctypes.byref(source),
        ctypes.byref(profile),
        ctypes.byref(profile_size),
        ctypes.byref(logon_id),
        ctypes.byref(token),
        ctypes.byref(quotas),
        ctypes.byref(sub_status),
    )
    if profile.value:
        api.secur32.LsaFreeReturnBuffer(profile)
    return status & 0xFFFFFFFF, token.value


def _token_user(api: SimpleNamespace, token: Any) -> Any:
    """The TOKEN_USER data of a token as a buffer, or None."""
    import ctypes
    from ctypes import wintypes

    needed = wintypes.DWORD(0)
    api.advapi32.GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        return None
    buffer = ctypes.create_string_buffer(needed.value)
    if not api.advapi32.GetTokenInformation(
        token, _TOKEN_USER_CLASS, buffer, needed.value, ctypes.byref(needed)
    ):
        return None
    return buffer


def _is_current_user(api: SimpleNamespace, t: SimpleNamespace, token: Any) -> bool:
    import ctypes
    from ctypes import wintypes

    process_token = wintypes.HANDLE()
    opened = api.advapi32.OpenProcessToken(
        api.kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(process_token)
    )
    if not opened:
        return False
    try:
        mine = _token_user(api, process_token)
        theirs = _token_user(api, token)
    finally:
        api.kernel32.CloseHandle(process_token)
    if mine is None or theirs is None:
        return False
    mine_sid = ctypes.cast(mine, ctypes.POINTER(t.TokenUser)).contents.User.Sid
    their_sid = ctypes.cast(theirs, ctypes.POINTER(t.TokenUser)).contents.User.Sid
    return bool(api.advapi32.EqualSid(mine_sid, their_sid))


def _logon_as_owner(
    api: SimpleNamespace,
    t: SimpleNamespace,
    lsa: Any,
    package_id: int,
    credentials: tuple[int, int],
) -> bool:
    status, token = _lsa_logon(api, t, lsa, package_id, credentials, _LOGON_INTERACTIVE)
    if status == _STATUS_LOGON_TYPE_NOT_GRANTED:
        status, token = _lsa_logon(api, t, lsa, package_id, credentials, _LOGON_NETWORK)
    if status != 0 or not token:
        return False
    try:
        return _is_current_user(api, t, token)
    finally:
        api.kernel32.CloseHandle(token)


def _confirm_windows_credentials(reason: str) -> bool:
    t = _win_types()
    api = _load_apis(t)
    lsa = _connect_lsa(api)
    try:
        package_id = _negotiate_package(api, t, lsa)
        credentials = _prompt_credentials(api, t, reason, package_id)
        if credentials is None:
            return False
        try:
            return _logon_as_owner(api, t, lsa, package_id, credentials)
        finally:
            _release_credentials(api, *credentials)
    finally:
        api.secur32.LsaDeregisterLogonProcess(lsa)


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
